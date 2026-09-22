# Telegram Multi-Bot Platform

## Render Environment Variables

BOT_TOKEN = platform/admin bot token
DATABASE_URL = Render PostgreSQL Internal Database URL
ADMIN_ID = platform admin Telegram ID
ENCRYPTION_KEY = Fernet key
RENDER_EXTERNAL_URL = https://YOUR-SERVICE.onrender.com
CARD_NUMBER = manual payment card
CARD_OWNER = card owner

## Generate ENCRYPTION_KEY

python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

## Important

- Child bot tokens are encrypted in PostgreSQL.
- API keys are encrypted in PostgreSQL.
- User-facing token/API displays are masked.
- Platform wallet is separate from bot-user wallets.
- Bot users are isolated by bot_id.
- Bot expiry suspends the bot but keeps data for 7 days.
- This project uses webhooks, not polling, so multiple child bots can run in one Render Web Service.

Provider-specific Stars/Premium endpoints must be entered through the API credential records or wired to the exact provider API contract. The generic API layer intentionally does not invent undocumented provider endpoints.
