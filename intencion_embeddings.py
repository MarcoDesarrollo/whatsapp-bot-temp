import os
import logging
import numpy as np
from dotenv import load_dotenv
from supabase import create_client
from openai import OpenAI

# ✅ Cargar variables de entorno
load_dotenv()

embedding_cache = {}
ejemplos_embeddings_cache = {}

# ✅ Inicializar Supabase y OpenAI
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Función de similitud coseno
def cosine_similarity(v1, v2):
    v1 = np.array(v1)
    v2 = np.array(v2)
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

# ✅ Embedding con caché
def obtener_embedding_cached(texto, cache_key=None):
    clave = cache_key or texto.strip().lower()
    if clave in embedding_cache:
        return embedding_cache[clave]

    try:
        res = client.embeddings.create(
            model="text-embedding-ada-002",
            input=texto
        )
        vector = res.data[0].embedding
        embedding_cache[clave] = vector
        return vector
    except Exception as e:
        logging.error(f"❌ Error generando embedding: {e}")
        return None

# ✅ Cargar ejemplos desde Supabase
def cargar_ejemplos_intencion(tipo, negocio="generico"):
    try:
        res = supabase.from_("ejemplos_intencion")\
            .select("ejemplo")\
            .eq("tipo", tipo)\
            .or_(f"negocio.eq.{negocio},negocio.eq.generico")\
            .execute()
        return [e["ejemplo"] for e in res.data or []]
    except Exception as e:
        logging.error(f"❌ Error cargando ejemplos: {e}")
        return []

# ✅ Analizar intención por embeddings
def analizar_intencion_con_embeddings(texto, tipo, negocio="generico", threshold=0.80, embedding_usuario=None):
    try:
        if embedding_usuario is None:
            embedding_usuario = obtener_embedding_cached(texto)
        if embedding_usuario is None:
            return False

        ejemplos = cargar_ejemplos_intencion(tipo, negocio)
        if not ejemplos:
            return False

        if tipo in ejemplos_embeddings_cache:
            embeddings_ejemplos = ejemplos_embeddings_cache[tipo]
        else:
            res = client.embeddings.create(model="text-embedding-ada-002", input=ejemplos)
            embeddings_ejemplos = [r.embedding for r in res.data]
            ejemplos_embeddings_cache[tipo] = embeddings_ejemplos

        for emb in embeddings_ejemplos:
            if cosine_similarity(embedding_usuario, emb) >= threshold:
                return True
        return False

    except Exception as e:
        logging.error(f"❌ Error analizando intención por embeddings: {e}")
        return False
