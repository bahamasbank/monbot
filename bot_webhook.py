#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, io, textwrap, traceback, aiosqlite
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler
)

# ---------- ENV & DB ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")  # facultatif en local
TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED = {int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip().isdigit()}
DB = os.getenv("DB_PATH", str(BASE_DIR / "data.db"))

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # ex: https://monbot.onrender.com
PORT = int(os.getenv("PORT", "10000"))              # Render fournit PORT automatiquement

MENU, ASK_COUNT, ASK_QUERY = range(3)
KB_MAIN = ReplyKeyboardMarkup(
    [["📱 Tirer des numéros", "🔎 Rechercher fiche"], ["📊 Statut"]],
    resize_keyboard=True,
)

def auth(user_id: int) -> bool:
    return (not ALLOWED) or (user_id in ALLOWED)

# ---------- Utils tel ----------
def normalize_phone(q: str) -> str:
    s = re.sub(r"[^\d+]", "", (q or "").strip())
    if s.startswith("0033"):
        s = "+" + s[2:]
    if s.startswith("+33"):
        d = re.sub(r"\D", "", s)
        if d.startswith("33") and len(d) == 11:
            return "+33" + d[2:]
        if d.startswith("33") and len(d) == 12 and d[2] == "0":
            return "+33" + d[3:]
        return "+33" + d[2:] if d.startswith("33") else s
    d = re.sub(r"\D", "", s)
    if len(d) == 10 and d.startswith("0"):
        return "+33" + d[1:]
    if len(d) == 11 and d.startswith("33"):
        return "+33" + d[2:]
    return ""

def last9_digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-9:] if len(d) >= 9 else d

# Nettoyage SQL pour comparer les 9 derniers chiffres dans une colonne "mobile" qui contient 1..n numéros
CLEAN_SQL = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "IFNULL(mobile,''),' ',''),'+',''),'-',''),'.',''),'(',''),')',''),',',''),';','')"
)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text("⛔ Accès refusé. Donne-moi ton ID pour whitelister.")
        return ConversationHandler.END
    await update.message.reply_text("Choisis une action :", reply_markup=KB_MAIN)
    return MENU

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if "tirer" in text or "numéro" in text or "📱" in (update.message.text or ""):
        await update.message.reply_text("Combien de numéros veux-tu ?", reply_markup=ReplyKeyboardRemove())
        return ASK_COUNT
    if "recherch" in text or "🔎" in (update.message.text or ""):
        await update.message.reply_text(
            "Envoie un *numéro* (+33… ou 0…) **ou** un *nom/prénom*.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return ASK_QUERY
    if "statut" in text or "📊" in (update.message.text or ""):
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT COUNT(*) FROM phones"); phc = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM people"); pplc = (await cur.fetchone())[0]
        await update.message.reply_text(f"📱 Numéros dispo: {phc}\n👥 Fiches: {pplc}", reply_markup=KB_MAIN)
        return MENU
    await update.message.reply_text("Choisis :", reply_markup=KB_MAIN)
    return MENU

def fmt_block_md(r) -> str:
    # r est un sqlite3.Row -> accès par clés
    def g(k): 
        v = r[k] if k in r.keys() else ""
        return "" if v is None else str(v)
    # adresse & postal
    address = f"{g('streetNumber')} {g('streetType')} {g('streetName')}".strip()
    postal  = f"{g('postalCode')} {g('city')}".strip()
    # nettoyer la date pour n'afficher que YYYY-MM-DD
    bd = g('birthDate')
    if "T" in bd:
        bd = bd.split("T", 1)[0]
    # échapper MarkdownV2
    def esc(s: str) -> str:
        return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', s or "")
    lines = [
        f"*Firstname* : {esc(g('firstname') or '-')}",
        f"*Lastname*  : {esc(g('lastname') or '-')}",
        f"*Email*     : {esc(g('email') or '-')}",
        f"*Mobile*    : {esc(g('mobile') or '-')}",
        f"*Address*   : {esc(address or '-')}",
        f"*Postal*    : {esc(postal or '-')}",
        f"*IBAN*      : {esc(g('iban') or '-')}",
        f"*BIC*       : {esc(g('bic') or '-')}",
        f"*BirthDate* : {esc(bd or '-')}",
        f"*Age*       : {esc(str(g('age') or ''))}",
    ]
    return "\n".join(lines)

async def ask_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    rows = []
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        # 1) par numéro ?
        if re.search(r"\d", q):
            cand1 = last9_digits(q)
            cand2 = last9_digits(normalize_phone(q))
            cands = [c for c in {cand1, cand2} if c]
            if cands:
                clause = " OR ".join([f"{CLEAN_SQL} LIKE '%' || ? || '%'" for _ in cands])
                sql = f"SELECT * FROM people WHERE {clause} LIMIT 20"
                async with db.execute(sql, cands) as cur:
                    rows = await cur.fetchall()
        # 2) sinon par nom/prénom
        if not rows:
            parts = [p for p in re.split(r"\s+", q) if p]
            if parts:
                sql = "SELECT * FROM people WHERE 1=1"
                params = []
                for p in parts:
                    sql += " AND (LOWER(firstname) LIKE ? OR LOWER(lastname) LIKE ?)"
                    params += [f"%{p.lower()}%", f"%{p.lower()}%"]
                sql += " LIMIT 20"
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text("Aucun résultat.")
        return ASK_QUERY

    for r in rows:
        await update.message.reply_text(fmt_block_md(r), parse_mode="MarkdownV2")
    await update.message.reply_text("Nouvelle recherche ?", reply_markup=KB_MAIN)
    return MENU

async def ask_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt.isdigit() or int(txt) <= 0:
        await update.message.reply_text("Entre un nombre entier > 0.")
        return ASK_COUNT
    n = int(txt)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, number FROM phones ORDER BY id ASC LIMIT ?", (n,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            await update.message.reply_text("Aucun numéro dispo.", reply_markup=KB_MAIN)
            return MENU
        ids = [r["id"] for r in rows]
        nums = [r["number"] for r in rows]
        await db.executemany("DELETE FROM phones WHERE id=?", [(i,) for i in ids])
        await db.commit()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"export_{stamp}.txt"
    buf = io.BytesIO(("\n".join(nums) + "\n").encode("utf-8")); buf.name = fname
    await update.message.reply_document(InputFile(buf, filename=fname),
        caption=f"📦 {len(nums)} numéros extraits.\nRestant consultable via 📊 Statut.")
    await update.message.reply_text("Tu veux autre chose ?", reply_markup=KB_MAIN)
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bye.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[ERROR]", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Oups, une erreur est survenue. Réessaie.")
    except Exception:
        pass

def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_count)],
            ASK_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_error_handler(error_handler)
    return app

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN manquant"); return
    app = build_app()

    if WEBHOOK_URL:
        # mode Render : webhook
        print(f"[BOOT] Starting webhook on 0.0.0.0:{PORT}")
        print(f"[BOOT] WEBHOOK_URL = {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
    else:
        # fallback (utile pour tester en local)
        print("[BOOT] WEBHOOK_URL absent → run_polling()")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
