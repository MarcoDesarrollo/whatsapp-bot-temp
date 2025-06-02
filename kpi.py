import os
import logging
from dotenv import load_dotenv
from supabase import create_client
from datetime import datetime, timezone

# ✅ Cargar variables de entorno
load_dotenv()

# ✅ Inicializar Supabase
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

# ✅ Función para registrar eventos KPI
def registrar_kpi_evento(conversation_id, tipo_evento, user_id=None, valor=None):
    try:
        evento = {
            "conversation_id": conversation_id,
            "tipo_evento": tipo_evento,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        if user_id:
            evento["user_id"] = user_id
        if valor:
            evento["valor"] = valor

        supabase.from_("kpi_conversaciones").insert(evento).execute()
        logging.info(f"📊 KPI registrado: {tipo_evento}")
    except Exception as e:
        logging.error(f"❌ Error registrando KPI: {e}")
