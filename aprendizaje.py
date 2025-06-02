import os
import logging
from dotenv import load_dotenv
from supabase import create_client

# ✅ Cargar variables de entorno
load_dotenv()

# ✅ Inicializar Supabase
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

# ✅ Función para guardar frases de conversión
def guardar_frase_conversion(user_id, mensaje, tipo):
    try:
        supabase.from_("frases_conversion").insert({
            "user_id": user_id,
            "mensaje": mensaje,
            "tipo": tipo
        }).execute()
        logging.info(f"🧠 Frase guardada como efectiva ({tipo})")
    except Exception as e:
        logging.error(f"❌ Error guardando frase de conversión: {e}")
