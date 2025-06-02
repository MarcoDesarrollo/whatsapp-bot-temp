import os
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client
from twilio.rest import Client as TwilioClient

load_dotenv()

# 🔐 Configuración
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH)

# 📩 Mensaje de seguimiento
MENSAJE_RECORDATORIO = (
    "📄 ¡Hola de nuevo! Queríamos saber si pudiste revisar la cotización que te enviamos. "
    "Si tienes alguna duda, estoy aquí para ayudarte."
)

def enviar_whatsapp(to: str, mensaje: str):
    try:
        message = twilio_client.messages.create(
            body=mensaje,
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=f"whatsapp:{to}"
        )
        logging.info(f"📨 Recordatorio enviado a {to}")
    except Exception as e:
        logging.error(f"❌ Error al enviar WhatsApp a {to}: {e}")

def verificar_cotizaciones_pendientes():
    try:
        ahora = datetime.now(timezone.utc)
        hace_20_min = ahora - timedelta(minutes=20)

        # 🔎 Buscar cotizaciones no respondidas que se enviaron hace más de 20 minutos
        resultado = supabase.from_("conversation_history")\
            .select("id, user_id, cotizacion_enviada_at, cotizacion_respondida")\
            .is_("cotizacion_respondida", None)\
            .gte("cotizacion_enviada_at", hace_20_min.isoformat())\
            .lte("cotizacion_enviada_at", (ahora - timedelta(minutes=5)).isoformat())\
            .execute()

        pendientes = resultado.data or []

        for row in pendientes:
            user_id = row["user_id"]
            enviar_whatsapp(user_id, MENSAJE_RECORDATORIO)

            # Opcional: marcar que ya se envió un recordatorio para no repetirlo
            supabase.from_("conversation_history").update({
                "cotizacion_respondida": False  # Lo puedes cambiar por otro campo como `recordatorio_enviado`
            }).eq("id", row["id"]).execute()

    except Exception as e:
        logging.error(f"❌ Error en cron de cotizaciones: {e}")

if __name__ == "__main__":
    verificar_cotizaciones_pendientes()
