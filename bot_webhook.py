#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import textwrap
import traceback
from datetime import datetime
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# Chargement .env en local (sur Render, les variables viennent de l'UI)
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DB = os.getenv("DB_PATH", str(BASE_DIR / "data.db")).strip()
ALLOWED = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))  # Render fournit PORT

# ──────────────────────────────────────────────────────────────────────────────
# Conversation states & clavier
# ──────────────────────────────────────────────────────────────────────────────
MENU, ASK_COUNT, ASK_QUERY = range(3)

KB_MAIN = ReplyKeyboardMarkup(
    [["📱 Tirer des numéros", "🔎 Rechercher fiche"], ["📊 Statut"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

def auth(user_id: int) -> bool:
    return (not ALLOWED) or (user_id in ALLOWED)

# ──────────────────────────────────────────────────────────────────────────────
# Utils téléphone & formatage
# ──────────────────────────────────────────────────────────────────────────────
def normalize_phone(q: str) -> str:
    """Normalise en +33XXXXXXXXX si possible, sinon renvoie ''."""
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

# Nettoyage SQL de la colonne mobile (retire espace, + - . ( ) , ; )
CLEAN_SQL = (
    "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
    "IFNULL(mobile,''),' ',''),'+',''),'-',''),'.',''),'(',''),')',''),',',''),';','')"
)

def esc_mdv2(t: str) -> str:
    """Échappe MarkdownV2."""
    if t is None:
        t = ""
    t = str(t)
    return re.sub(r'([_*\[\]()~`>#+\-=\|{}.!])', r'\\\1', t)

def trim_birthdate(bd: str) -> str:
    """Garde YYYY-MM-DD si fourni en ISO complet."""
    if not bd:
        return ""
    # ex: 1969-12-15T00:00:00+01:00 -> 1969-12-15
    return bd.split("T", 1)[0]

def fmt_block_md(row: "aiosqlite.Row") -> str:
    """Bloc propre en MarkdownV2 (une info par ligne)."""
    g = lambda k: (row[k] if row[k] is not None else "")
    mobile = g("mobile") or ""
    mobile = re.sub(r"\s*,\s*", ", ", mobile.strip())  # espace après virgules

    address = f"{g('streetNumber')} {g('streetType')} {g('streetName')}".strip()
    postal  = f"{g('postalCode')} {g('city')}".strip()
    birth   = trim_birthdate(g("birthDate"))

    lines = [
        f"*Firstname* : {esc_mdv2(g('firstname') or '-')}",
        f"*Lastname*  : {esc_mdv2(g('lastname') or '-')}",
        f"*Email*     : {esc_mdv2(g('email') or '-')}",
        f"*Mobile*    : {esc_mdv2(mobile or '-')}",
        f"*Address*   : {esc_mdv2(address or '-')}",
        f"*Postal*    : {esc_mdv2(postal or '-')}",
        f"*IBAN*      : {esc_mdv2(g('iban') or '-')}",
        f"*BIC*       : {esc_mdv2(g('bic') or '-')}",
        f"*BirthDate* : {esc_mdv2(birth or '-')}",
        f"*Age*       : {esc_mdv2(g('age') or '-')}",
    ]
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────
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
            reply_markup=ReplyKeyboardRemove(),
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
        caption=f"📦 {len(nums)} numéros extraits.\nRestant consultable via 📊 Statut.",
    )
    await update.message.reply_text("Tu veux autre chose ?", reply_markup=KB_MAIN)
    return MENU

async def ask_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not auth(uid):
        await update.message.reply_text("⛔ Accès refusé.")
        return ConversationHandler.END

    q = (update.message.text or "").strip()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        rows = []

        # 1) Tentative par numéro si la saisie contient des chiffres
        if re.search(r"\d", q):
            cand1 = last9_digits(q)            # 9 derniers chiffres saisie brute
            norm  = normalize_phone(q)
            cand2 = last9_digits(norm) if norm else ""
            cands = [c for c in {cand1, cand2} if c]  # uniques, non vides
            if cands:
                clause = " OR ".join([f"{CLEAN_SQL} LIKE '%' || ? || '%'" for _ in cands])
                sql = f"SELECT * FROM people WHERE {clause} LIMIT 20"
                async with db.execute(sql, cands) as cur:
                    rows = await cur.fetchall()

        # 2) Sinon (ou pas trouvé), recherche par nom/prénom
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

    # Envoi un bloc par message (MarkdownV2)
    for r in rows:
        await update.message.reply_text(fmt_block_md(r), parse_mode="MarkdownV2")
    await update.message.reply_text("Nouvelle recherche ?", reply_markup=KB_MAIN)
    return MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bye.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────────────────────────
# Error handler (log propre)
# ──────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[ERROR]", "".join(traceback.format_exception(None, context.error, context.error.__traceback__)))
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Oups, une erreur est survenue. Réessaie.")
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# MAIN (WEBHOOK)  — Compatible Render (PORT + WEBHOOK_URL)
# ──────────────────────────────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN manquant")
        return

    # Détermine automatiquement le chemin d’écoute :
    # - si WEBHOOK_URL se termine par le token -> écoute sur /<token>
    # - sinon -> écoute sur /
    url_path = ""
    if WEBHOOK_URL and WEBHOOK_URL.endswith(TOKEN):
        url_path = TOKEN

    print(f"[BOOT] DB={DB}")
    print(f"[BOOT] PORT={PORT}")
    print(f"[BOOT] WEBHOOK_URL={WEBHOOK_URL or '(vide)'}")
    print(f"[BOOT] url_path={'/'+url_path if url_path else '/'}")

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

    # Lance le serveur webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,               # Render impose d'écouter sur ce port
        url_path=url_path,       # "" = racine | "<token>" = /<token>
        webhook_url=WEBHOOK_URL, # L’URL publique configurée sur Render
        drop_pending_updates=True,
        allowed_updates=None,
    )

if __name__ == "__main__":
    main()
