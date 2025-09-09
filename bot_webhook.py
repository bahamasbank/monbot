#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import textwrap
import traceback
import aiosqlite
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler
)
from pathlib import Path

# --- Chargement .env + chemin DB robustes ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")
TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED = {int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip().isdigit()}
DB = os.getenv("DB_PATH", str(BASE_DIR / "data.db"))

MENU, ASK_COUNT, ASK_QUERY = range(3)

KB_MAIN = ReplyKeyboardMarkup(
    [["📱 Tirer des numéros", "🔎 Rechercher fiche"], ["📊 Statut"]],
    resize_keyboard=True,
    one_time_keyboard=False
)

def auth(user_id: int) -> bool:
    return (not ALLOWED) or (user_id in ALLOWED)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text("⛔ Accès refusé. Donne-moi ton ID pour whitelister.")
        print(f"[WARN] user not allowed: {uid}")
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
            cur = await db.execute("SELECT COUNT(*) FROM phones")
            phc = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM people")
            pplc = (await cur.fetchone())[0]
        await update.message.reply_text(f"📱 Numéros dispo: {phc}\n👥 Fiches: {pplc}", reply_markup=KB_MAIN)
        return MENU
    await update.message.reply_text("Choisis :", reply_markup=KB_MAIN)
    return MENU

# --------- Utils téléphone ---------
def normalize_phone(q: str) -> str:
    s = re.sub(r"[^\d+]", "", q.strip())
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

# Nettoyage SQL simple et compatible (retire espace, + - . ( ) , ; )
CLEAN_SQL = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "IFNULL(mobile,''),' ',''),'+',''),'-',''),'.',''),'(',''),')',''),',',''),';','')"
)

# --------- Helpers affichage MarkdownV2 ---------
MDV2_ESCAPER = re.compile(r"([_\*\[\]\(\)~`>#+\-=\|{}\.\!\\])")

def esc(s: str) -> str:
    if s is None:
        s = "-"
    s = str(s)
    return MDV2_ESCAPER.sub(r"\\\1", s)

def getv(row, key: str) -> str:
    try:
        val = row[key]
        return "" if val is None else str(val)
    except Exception:
        return ""

def clean_birthdate(s: str) -> str:
    if not s:
        return "-"
    return s.split("T", 1)[0] if "T" in s else s

def build_address(r) -> str:
    parts = [getv(r, "streetNumber"), getv(r, "streetType"), getv(r, "streetName")]
    out = " ".join(p for p in parts if p).strip()
    return out if out else "-"

def build_postal(r) -> str:
    pc = getv(r, "postalCode").strip()
    city = getv(r, "city").strip()
    out = f"{pc} {city}".strip()
    return out if out else "-"

def normalize_mobile_display(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "-"
    return re.sub(r"\s*,\s*", ", ", s)

def fmt_block_md(r) -> str:
    lines = [
        f"*Firstname* : {esc(getv(r, 'firstname') or '-')}",
        f"*Lastname*  : {esc(getv(r, 'lastname') or '-')}",
        f"*Email*     : {esc(getv(r, 'email') or '-')}",
        f"*Mobile*    : {esc(normalize_mobile_display(getv(r, 'mobile')))}",
        f"*Address*   : {esc(build_address(r))}",
        f"*Postal*    : {esc(build_postal(r))}",
        f"*IBAN*      : {esc(getv(r, 'iban') or '-')}",
        f"*BIC*       : {esc(getv(r, 'bic') or '-')}",
        f"*BirthDate* : {esc(clean_birthdate(getv(r, 'birthDate')))}",
        f"*Age*       : {esc(getv(r, 'age') or '-')}",
    ]
    return "\n".join(lines)

# --------- Tirage de numéros ---------
async def ask_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text("⛔ Accès refusé.")
        return ConversationHandler.END

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

    buf = io.BytesIO(("\n".join(nums) + "\n").encode("utf-8"))
    buf.name = fname

    await update.message.reply_document(
        document=InputFile(buf, filename=fname),
        caption=f"📦 {len(nums)} numéros extraits.\nRestant consultable via 📊 Statut."
    )
    await update.message.reply_text("Tu veux autre chose ?", reply_markup=KB_MAIN)
    return MENU

# --------- Recherche fiches ---------
async def ask_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text("⛔ Accès refusé.")
        return ConversationHandler.END

    q = (update.message.text or "").strip()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        rows = []

        # Si la saisie contient des chiffres → tentative par numéro
        if re.search(r"\d", q):
            cand1 = last9_digits(q)
            norm  = normalize_phone(q)
            cand2 = last9_digits(norm) if norm else ""
            cands = [c for c in {cand1, cand2} if c]

            if cands:
                clause = " OR ".join([f"{CLEAN_SQL} LIKE '%' || ? || '%'" for _ in cands])
                sql = f"SELECT * FROM people WHERE {clause} LIMIT 20"
                async with db.execute(sql, cands) as cur:
                    rows = await cur.fetchall()

        # Sinon (ou si pas trouvé), recherche par nom/prénom
        if not rows:
            parts = [p for p in re.split(r"\s+", q) if p]
            if not parts:
                await update.message.reply_text("Envoie un numéro ou un nom.")
                return ASK_QUERY
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

    # Envoie UNE fiche par message, en MarkdownV2 propre
    for r in rows:
        await update.message.reply_text(fmt_block_md(r), parse_mode="MarkdownV2")

    await update.message.reply_text("Nouvelle recherche ?", reply_markup=KB_MAIN)
    return MENU

# --------- Error handler ---------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[ERROR]", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Oups, une erreur est survenue. Réessaie.")
    except Exception:
        pass

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN manquant")
        return

    application = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_count)],
            ASK_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_query)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Bye."))],
        allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_error_handler(error_handler)

    # --- Webhook mode (Render) ---
    PORT = int(os.environ.get("PORT", "8000"))  # Render fournit $PORT
    BASE_URL = os.environ.get("WEBHOOK_URL")    # ex: https://monbot.onrender.com
    PATH_SECRET = os.environ.get("WEBHOOK_PATH", TOKEN)  # chemin secret (par défaut, ton token)

    if not BASE_URL:
        print("❌ WEBHOOK_URL manquant (ex: https://monbot.onrender.com)")
        return

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=PATH_SECRET,
        webhook_url=f"{BASE_URL.rstrip('/')}/{PATH_SECRET}",
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
