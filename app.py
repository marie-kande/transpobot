"""
TranspoBot — Squelette Backend FastAPI
Projet GLSi L3 — ESP/UCAD
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
import os
from dotenv import load_dotenv
load_dotenv()
import re
import httpx

app = FastAPI(title="TranspoBot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Configuration ──────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "transpobot"),
}

LLM_API_KEY  = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL    = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")

# ── Schéma de la base (pour le prompt système) ─────────────────
DB_SCHEMA = """
Tables MySQL disponibles :

vehicules(id, immatriculation, type[bus/minibus/taxi], capacite, statut[actif/maintenance/hors_service], kilometrage, date_acquisition)
chauffeurs(id, nom, prenom, telephone, numero_permis, categorie_permis, disponibilite, vehicule_id, date_embauche)
lignes(id, code, nom, origine, destination, distance_km, duree_minutes)
tarifs(id, ligne_id, type_client[normal/etudiant/senior], prix)
trajets(id, ligne_id, chauffeur_id, vehicule_id, date_heure_depart, date_heure_arrivee, statut[planifie/en_cours/termine/annule], nb_passagers, recette)
incidents(id, trajet_id, type[panne/accident/retard/autre], description, gravite[faible/moyen/grave], date_incident, resolu)
"""

SYSTEM_PROMPT = f"""Tu es TranspoBot, l'assistant intelligent de la compagnie de transport urbain de Dakar.
Tu aides les gestionnaires à interroger la base de données en langage naturel (français ou anglais).

{DB_SCHEMA}

RÈGLES ABSOLUES (à ne jamais enfreindre) :
1. Tu génères UNIQUEMENT des requêtes SELECT. Toute requête INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE est INTERDITE.
2. Tu réponds TOUJOURS en JSON strict avec ce format exact :
   {{"sql": "SELECT ...", "explication": "Phrase claire en français décrivant le résultat"}}
3. Si la question ne nécessite pas de SQL (salutation, question hors-sujet), réponds :
   {{"sql": null, "explication": "Ta réponse en langue naturelle"}}
4. Toujours ajouter LIMIT 100 si non spécifié.
5. Utiliser des alias lisibles : chauffeur_nom, nb_incidents, recette_totale, etc.
6. Pour les dates relatives : utiliser DATE_SUB(NOW(), INTERVAL N DAY/MONTH).
7. Ne jamais exposer les mots de passe, tokens ou données sensibles.

EXEMPLES DE REQUÊTES ATTENDUES :
- "Combien de trajets cette semaine ?" → SELECT COUNT(*) as nb_trajets FROM trajets WHERE date_heure_depart >= DATE_SUB(NOW(), INTERVAL 7 DAY) AND statut='termine'
- "Quel chauffeur a le plus d'incidents ?" → SELECT c.nom, c.prenom, COUNT(i.id) as nb_incidents FROM incidents i JOIN trajets t ON i.trajet_id=t.id JOIN chauffeurs c ON t.chauffeur_id=c.id GROUP BY c.id ORDER BY nb_incidents DESC LIMIT 1
- "Véhicules en maintenance ?" → SELECT immatriculation, kilometrage, date_acquisition FROM vehicules WHERE statut='maintenance'
"""
# ── Connexion MySQL ────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def execute_query(sql: str):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

# ── Appel LLM ─────────────────────────────────────────────────
async def ask_llm(question: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": question},
                ],
                "temperature": 0,
                "max_tokens": 1000,
            },
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        # Extraire le JSON de la réponse
        import json
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError("Réponse LLM invalide")
def is_safe_sql(sql: str) -> bool:
    """Vérifie que la requête est bien un SELECT et ne contient rien de dangereux."""
    if not sql:
        return False
    sql_upper = sql.strip().upper()
    # Doit commencer par SELECT
    if not sql_upper.startswith("SELECT"):
        return False
    # Mots-clés interdits
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
                 "CREATE", "GRANT", "EXEC", "EXECUTE", "--", ";--"]
    return not any(kw in sql_upper for kw in forbidden)

# ── Routes API ─────────────────────────────────────────────────
class ChatMessage(BaseModel):
    question: str

@app.post("/api/chat")
async def chat(msg: ChatMessage):
    try:
        llm_response = await ask_llm(msg.question)
        sql = llm_response.get("sql")
        explication = llm_response.get("explication", "")

        if not sql:
            return {"answer": explication, "data": [], "sql": None}

        #  Validation de sécurité
        if not is_safe_sql(sql):
            raise HTTPException(status_code=400, 
                detail="Requête non autorisée : seules les requêtes SELECT sont permises.")

        data = execute_query(sql)
        return {"answer": explication, "data": data, "sql": sql, "count": len(data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
def get_stats():
    """Tableau de bord — statistiques rapides"""
    stats = {}
    queries = {
        "total_trajets":    "SELECT COUNT(*) as n FROM trajets WHERE statut='termine'",
        "trajets_en_cours": "SELECT COUNT(*) as n FROM trajets WHERE statut='en_cours'",
        "vehicules_actifs": "SELECT COUNT(*) as n FROM vehicules WHERE statut='actif'",
        "incidents_ouverts":"SELECT COUNT(*) as n FROM incidents WHERE resolu=FALSE",
        "recette_totale":   "SELECT COALESCE(SUM(recette),0) as n FROM trajets WHERE statut='termine'",
    }
    for key, sql in queries.items():
        result = execute_query(sql)
        stats[key] = result[0]["n"] if result else 0
    return stats

@app.get("/api/vehicules")
def get_vehicules():
    return execute_query("SELECT * FROM vehicules ORDER BY immatriculation")

@app.get("/api/chauffeurs")
def get_chauffeurs():
    return execute_query("""
        SELECT c.*, v.immatriculation
        FROM chauffeurs c
        LEFT JOIN vehicules v ON c.vehicule_id = v.id
        ORDER BY c.nom
    """)

@app.get("/api/trajets/recent")
def get_trajets_recent():
    return execute_query("""
        SELECT t.*, l.nom as ligne, ch.nom as chauffeur_nom,
               v.immatriculation
        FROM trajets t
        JOIN lignes l ON t.ligne_id = l.id
        JOIN chauffeurs ch ON t.chauffeur_id = ch.id
        JOIN vehicules v ON t.vehicule_id = v.id
        ORDER BY t.date_heure_depart DESC
        LIMIT 20
    """)

@app.get("/health")
def health():
    return {"status": "ok", "app": "TranspoBot"}

# ── Lancement ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
