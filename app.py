from flask import Flask, render_template, request
import requests
import sqlite3
import json
from datetime import datetime, timedelta

app = Flask(__name__)

# =========================================================
# CONFIGURACIÓN DE API
# =========================================================
FD_API_TOKEN = "22c2dc4063974893bac2d273b99b9e22"
FD_BASE_URL = "https://api.football-data.org/v4"
FD_HEADERS = {
    "X-Auth-Token": FD_API_TOKEN
}

MAX_TEAMS_LEAGUE_SCAN = 8
MAX_FIXTURES_TODAY_SCAN = 4

CACHE_DB = "cache.db"
TEAM_MATCHES_TTL_HOURS = 12
LEAGUE_FIXTURES_TTL_HOURS = 1

# =========================================================
# LIGAS DISPONIBLES
# =========================================================
FOOTBALL_DATA_LEAGUES = {
    "SA": "Serie A (Italia)",
    "PL": "Premier League (Inglaterra)",
    "PD": "LaLiga (España)",
    "BL1": "Bundesliga (Alemania)",
    "FL1": "Ligue 1 (Francia)",
    "PPL": "Liga Portugal (Portugal)",
    "CL": "UEFA Champions League",
    "EC": "Eurocopa (UEFA European Championship)"
}

# =========================================================
# BASE DE DATOS CACHE
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_cache_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_matches_cache (
            team_id INTEGER NOT NULL,
            limit_value INTEGER NOT NULL,
            payload TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (team_id, limit_value)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS league_fixtures_cache (
            league_code TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            payload TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (league_code, date_from, date_to)
        )
    """)

    conn.commit()
    conn.close()

init_cache_db()

def utc_now_iso():
    return datetime.utcnow().isoformat()


def is_cache_valid(saved_at_iso: str, ttl_hours: int) -> bool:
    try:
        saved_at = datetime.fromisoformat(saved_at_iso)
    except ValueError:
        return False

    return datetime.utcnow() - saved_at < timedelta(hours=ttl_hours)


def get_cached_team_matches(team_id: int, limit_value: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT payload, saved_at
        FROM team_matches_cache
        WHERE team_id = ? AND limit_value = ?
    """, (team_id, limit_value))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    if not is_cache_valid(row["saved_at"], TEAM_MATCHES_TTL_HOURS):
        return None

    return json.loads(row["payload"])


def set_cached_team_matches(team_id: int, limit_value: int, payload):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO team_matches_cache (team_id, limit_value, payload, saved_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(team_id, limit_value)
        DO UPDATE SET payload=excluded.payload, saved_at=excluded.saved_at
    """, (
        team_id,
        limit_value,
        json.dumps(payload),
        utc_now_iso()
    ))
    conn.commit()
    conn.close()


def get_cached_league_fixtures(league_code: str, date_from: str, date_to: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT payload, saved_at
        FROM league_fixtures_cache
        WHERE league_code = ? AND date_from = ? AND date_to = ?
    """, (league_code, date_from, date_to))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    if not is_cache_valid(row["saved_at"], LEAGUE_FIXTURES_TTL_HOURS):
        return None

    return json.loads(row["payload"])


def set_cached_league_fixtures(league_code: str, date_from: str, date_to: str, payload):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO league_fixtures_cache (league_code, date_from, date_to, payload, saved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(league_code, date_from, date_to)
        DO UPDATE SET payload=excluded.payload, saved_at=excluded.saved_at
    """, (
        league_code,
        date_from,
        date_to,
        json.dumps(payload),
        utc_now_iso()
    ))
    conn.commit()
    conn.close()


# =========================================================
# HELPERS GENERALES
# =========================================================
def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def fd_get(endpoint: str, params=None):
    url = f"{FD_BASE_URL}{endpoint}"
    response = requests.get(url, headers=FD_HEADERS, params=params or {}, timeout=30)
    response.raise_for_status()
    return response.json()


def fecha_hoy_local():
    return datetime.now().date()


def obtener_rango_fechas(selector_rango: str):
    hoy = fecha_hoy_local()

    if selector_rango == "manana":
        inicio = hoy + timedelta(days=1)
        fin = inicio
        etiqueta = "Mañana"
    elif selector_rango == "3dias":
        inicio = hoy
        fin = hoy + timedelta(days=2)
        etiqueta = "Próximos 3 días"
    else:
        inicio = hoy
        fin = hoy
        etiqueta = "Hoy"

    return {
        "date_from": inicio.isoformat(),
        "date_to": fin.isoformat(),
        "label": etiqueta
    }


def fd_obtener_temporada_actual(codigo_liga: str):
    data = fd_get(f"/competitions/{codigo_liga}")
    temporada = data.get("currentSeason", {})
    inicio = temporada.get("startDate", "")[:4]
    fin = temporada.get("endDate", "")[:4]

    if inicio and fin:
        return f"{inicio}-{fin}"
    return "-"


# =========================================================
# ESTADÍSTICAS
# =========================================================
def calcular_indicadores_desde_partidos(partidos):
    victorias = 0
    empates = 0
    derrotas = 0
    no_perdio = 0
    over15 = 0
    over25 = 0
    btts = 0
    marco1 = 0
    recibio1 = 0

    total = len(partidos)
    if total == 0:
        return None

    for p in partidos:
        goles_equipo = p["goles_equipo"]
        goles_oponente = p["goles_oponente"]

        if goles_equipo > goles_oponente:
            victorias += 1
        elif goles_equipo == goles_oponente:
            empates += 1
        else:
            derrotas += 1

        if goles_equipo >= goles_oponente:
            no_perdio += 1

        if goles_equipo + goles_oponente > 1:
            over15 += 1

        if goles_equipo + goles_oponente > 2:
            over25 += 1

        if goles_equipo > 0 and goles_oponente > 0:
            btts += 1

        if goles_equipo > 0:
            marco1 += 1

        if goles_oponente > 0:
            recibio1 += 1

    indicadores = {
        "Victoria": round(victorias / total * 100),
        "Empate": round(empates / total * 100),
        "Derrota": round(derrotas / total * 100),
        "No perdió": round(no_perdio / total * 100),
        "Más de 1.5 goles": round(over15 / total * 100),
        "Más de 2.5 goles": round(over25 / total * 100),
        "Ambos anotan": round(btts / total * 100),
        "Marcó al menos 1 gol": round(marco1 / total * 100),
        "Recibió al menos 1 gol": round(recibio1 / total * 100),
    }

    return {
        "total": total,
        "indicadores": indicadores,
        "partidos": partidos
    }


def resumir_perfil_equipo(partidos):
    total = len(partidos)
    if total == 0:
        return None

    marco_1 = 0
    recibio_1 = 0
    under_45 = 0
    no_perdio = 0
    no_gano = 0
    ht_validos = 0
    over05_ht = 0

    for p in partidos:
        gf = p["goles_equipo"]
        gc = p["goles_oponente"]
        ht_total = p.get("ht_total")

        if gf >= 1:
            marco_1 += 1
        if gc >= 1:
            recibio_1 += 1
        if (gf + gc) < 5:
            under_45 += 1
        if gf >= gc:
            no_perdio += 1
        if gf <= gc:
            no_gano += 1

        if ht_total is not None:
            ht_validos += 1
            if ht_total >= 1:
                over05_ht += 1

    return {
        "marco_1": round(marco_1 / total * 100),
        "recibio_1": round(recibio_1 / total * 100),
        "under_45": round(under_45 / total * 100),
        "no_perdio": round(no_perdio / total * 100),
        "no_gano": round(no_gano / total * 100),
        "over05_ht": round(over05_ht / ht_validos * 100) if ht_validos > 0 else 0,
        "total": total
    }


def combinar_items_partido(local_nombre, visitante_nombre, perfil_local, perfil_visitante):
    eventos = [
        {
            "nombre": f"{local_nombre} marca 1+ gol",
            "valor": round((perfil_local["marco_1"] + perfil_visitante["recibio_1"]) / 2)
        },
        {
            "nombre": f"{visitante_nombre} marca 1+ gol",
            "valor": round((perfil_visitante["marco_1"] + perfil_local["recibio_1"]) / 2)
        },
        {
            "nombre": "Más de 0.5 goles al descanso",
            "valor": round((perfil_local["over05_ht"] + perfil_visitante["over05_ht"]) / 2)
        },
        {
            "nombre": "Menos de 4.5 goles",
            "valor": round((perfil_local["under_45"] + perfil_visitante["under_45"]) / 2)
        },
        {
            "nombre": "Doble oportunidad local o empate",
            "valor": round((perfil_local["no_perdio"] + perfil_visitante["no_gano"]) / 2)
        },
        {
            "nombre": "Doble oportunidad visitante o empate",
            "valor": round((perfil_visitante["no_perdio"] + perfil_local["no_gano"]) / 2)
        }
    ]

    eventos.sort(key=lambda x: x["valor"], reverse=True)
    return eventos


# =========================================================
# EQUIPOS Y PARTIDOS
# =========================================================
def fd_buscar_equipo_por_nombre(nombre_equipo: str):
    nombre_buscado = normalize_text(nombre_equipo)

    for codigo in FOOTBALL_DATA_LEAGUES.keys():
        data = fd_get(f"/competitions/{codigo}/teams")
        teams = data.get("teams", [])

        for team in teams:
            nombre = normalize_text(team.get("name"))
            short_name = normalize_text(team.get("shortName"))
            tla = normalize_text(team.get("tla"))

            if nombre_buscado == nombre or nombre_buscado == short_name or nombre_buscado == tla:
                return {
                    "id": team["id"],
                    "name": team["name"]
                }

        for team in teams:
            nombre = normalize_text(team.get("name"))
            short_name = normalize_text(team.get("shortName"))

            if nombre_buscado in nombre or nombre_buscado in short_name:
                return {
                    "id": team["id"],
                    "name": team["name"]
                }

    return None


def fd_obtener_partidos_equipo(team_id: int, limite: int = 10):
    cached = get_cached_team_matches(team_id, limite)
    if cached is not None:
        return cached

    data = fd_get(f"/teams/{team_id}/matches", {
        "status": "FINISHED",
        "limit": limite
    })
    matches = data.get("matches", [])
    set_cached_team_matches(team_id, limite, matches)
    return matches


def fd_convertir_matches_a_partidos(matches, team_name):
    partidos = []
    team_name_norm = normalize_text(team_name)

    for m in matches:
        home_team = m.get("homeTeam", {}).get("name", "")
        away_team = m.get("awayTeam", {}).get("name", "")

        score = m.get("score", {})
        full_time = score.get("fullTime", {})
        half_time = score.get("halfTime", {})

        goles_local = full_time.get("home")
        goles_visita = full_time.get("away")

        ht_local = half_time.get("home")
        ht_visita = half_time.get("away")

        if goles_local is None or goles_visita is None:
            continue

        ht_total = None
        if ht_local is not None and ht_visita is not None:
            ht_total = ht_local + ht_visita

        if normalize_text(home_team) == team_name_norm:
            goles_equipo = goles_local
            goles_oponente = goles_visita
            oponente = away_team
        elif normalize_text(away_team) == team_name_norm:
            goles_equipo = goles_visita
            goles_oponente = goles_local
            oponente = home_team
        else:
            continue

        partidos.append({
            "equipo": team_name,
            "oponente": oponente,
            "goles_equipo": goles_equipo,
            "goles_oponente": goles_oponente,
            "ht_total": ht_total
        })

    return partidos


def fd_obtener_equipos_liga(codigo_liga: str):
    data = fd_get(f"/competitions/{codigo_liga}/teams")
    equipos = []

    for team in data.get("teams", []):
        equipos.append({
            "id": team.get("id"),
            "name": team.get("name")
        })

    return equipos


def fd_obtener_partidos_rango_liga(codigo_liga: str, date_from: str, date_to: str):
    cached = get_cached_league_fixtures(codigo_liga, date_from, date_to)
    if cached is not None:
        return cached

    data = fd_get(f"/competitions/{codigo_liga}/matches", {
        "dateFrom": date_from,
        "dateTo": date_to
    })
    matches = data.get("matches", [])
    set_cached_league_fixtures(codigo_liga, date_from, date_to, matches)
    return matches


# =========================================================
# RUTAS
# =========================================================
@app.route("/")
def inicio():
    return render_template(
        "index.html",
        football_data_leagues=FOOTBALL_DATA_LEAGUES
    )


@app.route("/analizar", methods=["POST"])
def analizar():
    equipo_ingresado = (request.form.get("equipo") or "").strip()
    cantidad_raw = request.form.get("cantidad") or "10"

    try:
        cantidad = int(cantidad_raw)
    except ValueError:
        cantidad = 10

    if not equipo_ingresado:
        return render_template(
            "resultado.html",
            equipo="",
            cantidad=0,
            error="Debes escribir un equipo."
        )

    try:
        equipo_encontrado = fd_buscar_equipo_por_nombre(equipo_ingresado)

        if not equipo_encontrado:
            return render_template(
                "resultado.html",
                equipo=equipo_ingresado,
                cantidad=0,
                error="No se encontró ese equipo."
            )

        matches = fd_obtener_partidos_equipo(equipo_encontrado["id"], cantidad)
        partidos = fd_convertir_matches_a_partidos(matches, equipo_encontrado["name"])

        if not partidos:
            return render_template(
                "resultado.html",
                equipo=equipo_encontrado["name"],
                cantidad=0,
                error="No se encontraron partidos para ese equipo."
            )

        analisis = calcular_indicadores_desde_partidos(partidos)
        if not analisis:
            return render_template(
                "resultado.html",
                equipo=equipo_encontrado["name"],
                cantidad=0,
                error="No hubo marcadores válidos para analizar."
            )

        total = analisis["total"]
        indicadores = analisis["indicadores"]

        puntos = []
        for p in partidos:
            if p["goles_equipo"] > p["goles_oponente"]:
                puntos.append(3)
            elif p["goles_equipo"] == p["goles_oponente"]:
                puntos.append(1)
            else:
                puntos.append(0)

        return render_template(
            "resultado.html",
            equipo=equipo_encontrado["name"],
            cantidad=total,
            victoria=indicadores["Victoria"],
            empate=indicadores["Empate"],
            derrota=indicadores["Derrota"],
            over15=indicadores["Más de 1.5 goles"],
            btts=indicadores["Ambos anotan"],
            partidos=partidos,
            puntos=puntos,
            error=None
        )

    except requests.HTTPError as e:
        return render_template(
            "resultado.html",
            equipo=equipo_ingresado,
            cantidad=0,
            error=f"Error HTTP al consultar la API: {e}"
        )
    except requests.RequestException as e:
        return render_template(
            "resultado.html",
            equipo=equipo_ingresado,
            cantidad=0,
            error=f"Error de conexión con la API: {e}"
        )
    except Exception as e:
        return render_template(
            "resultado.html",
            equipo=equipo_ingresado,
            cantidad=0,
            error=f"Ocurrió un error inesperado: {e}"
        )


@app.route("/analizar-liga", methods=["POST"])
def analizar_liga():
    liga = (request.form.get("liga") or "").strip()
    cantidad_raw = request.form.get("cantidad") or "10"
    umbral_raw = request.form.get("umbral") or "90"

    try:
        cantidad = int(cantidad_raw)
    except ValueError:
        cantidad = 10

    try:
        umbral = int(umbral_raw)
    except ValueError:
        umbral = 90

    try:
        if liga not in FOOTBALL_DATA_LEAGUES:
            return render_template(
                "liga_resultado.html",
                league_name="Liga no válida",
                season="-",
                cantidad=0,
                umbral=umbral,
                resultados=[],
                scanned_teams=0,
                max_teams=MAX_TEAMS_LEAGUE_SCAN,
                error="La liga seleccionada no está soportada."
            )

        league_name = FOOTBALL_DATA_LEAGUES[liga]
        season_actual = fd_obtener_temporada_actual(liga)
        equipos = fd_obtener_equipos_liga(liga)

        if not equipos:
            return render_template(
                "liga_resultado.html",
                league_name=league_name,
                season=season_actual,
                cantidad=0,
                umbral=umbral,
                resultados=[],
                scanned_teams=0,
                max_teams=MAX_TEAMS_LEAGUE_SCAN,
                error="No se encontraron equipos para esa liga."
            )

        resultados = []
        equipos_a_escanear = equipos[:MAX_TEAMS_LEAGUE_SCAN]

        for team in equipos_a_escanear:
            team_id = team.get("id")
            team_name = team.get("name")

            matches = fd_obtener_partidos_equipo(team_id, cantidad)
            partidos = fd_convertir_matches_a_partidos(matches, team_name)
            analisis = calcular_indicadores_desde_partidos(partidos)

            if not analisis:
                continue

            mejor = max(analisis["indicadores"].items(), key=lambda x: x[1])

            resultados.append({
                "equipo": team_name,
                "partidos_analizados": analisis["total"],
                "mejor_indicador": mejor[0],
                "mejor_valor": mejor[1],
                "indicadores_superados": [
                    {"nombre": nombre, "valor": valor}
                    for nombre, valor in analisis["indicadores"].items()
                    if valor >= umbral
                ]
            })

        resultados.sort(key=lambda x: (x["mejor_valor"], x["equipo"]), reverse=True)

        return render_template(
            "liga_resultado.html",
            league_name=league_name,
            season=season_actual,
            cantidad=cantidad,
            umbral=umbral,
            resultados=resultados,
            scanned_teams=len(equipos_a_escanear),
            max_teams=MAX_TEAMS_LEAGUE_SCAN,
            error=None
        )

    except requests.HTTPError as e:
        return render_template(
            "liga_resultado.html",
            league_name="Error",
            season="-",
            cantidad=0,
            umbral=umbral,
            resultados=[],
            scanned_teams=0,
            max_teams=MAX_TEAMS_LEAGUE_SCAN,
            error=f"Error HTTP al consultar la API: {e}"
        )
    except requests.RequestException as e:
        return render_template(
            "liga_resultado.html",
            league_name="Error",
            season="-",
            cantidad=0,
            umbral=umbral,
            resultados=[],
            scanned_teams=0,
            max_teams=MAX_TEAMS_LEAGUE_SCAN,
            error=f"Error de conexión con la API: {e}"
        )
    except Exception as e:
        return render_template(
            "liga_resultado.html",
            league_name="Error",
            season="-",
            cantidad=0,
            umbral=umbral,
            resultados=[],
            scanned_teams=0,
            max_teams=MAX_TEAMS_LEAGUE_SCAN,
            error=f"Ocurrió un error inesperado: {e}"
        )


@app.route("/partidos-hoy", methods=["POST"])
def partidos_hoy():
    liga = (request.form.get("liga") or "").strip()
    rango = (request.form.get("rango") or "hoy").strip()
    cantidad_raw = request.form.get("cantidad") or "5"
    umbral_raw = request.form.get("umbral") or "90"

    try:
        cantidad = int(cantidad_raw)
    except ValueError:
        cantidad = 5

    try:
        umbral = int(umbral_raw)
    except ValueError:
        umbral = 90

    try:
        if liga not in FOOTBALL_DATA_LEAGUES:
            return render_template(
                "partidos_hoy.html",
                league_name="Liga no válida",
                season="-",
                fecha_label="-",
                cantidad=cantidad,
                umbral=umbral,
                partidos=[],
                scanned_matches=0,
                max_matches=MAX_FIXTURES_TODAY_SCAN,
                error="La liga seleccionada no está soportada."
            )

        league_name = FOOTBALL_DATA_LEAGUES[liga]
        season_actual = fd_obtener_temporada_actual(liga)

        rango_fechas = obtener_rango_fechas(rango)
        fixtures = fd_obtener_partidos_rango_liga(
            liga,
            rango_fechas["date_from"],
            rango_fechas["date_to"]
        )

        if not fixtures:
            return render_template(
                "partidos_hoy.html",
                league_name=league_name,
                season=season_actual,
                fecha_label=rango_fechas["label"],
                cantidad=cantidad,
                umbral=umbral,
                partidos=[],
                scanned_matches=0,
                max_matches=MAX_FIXTURES_TODAY_SCAN,
                error="No hay partidos en ese rango para esa liga."
            )

        partidos_hoy_lista = []
        fixtures_a_escanear = fixtures[:MAX_FIXTURES_TODAY_SCAN]

        for f in fixtures_a_escanear:
            local = f.get("homeTeam", {}) or {}
            visitante = f.get("awayTeam", {}) or {}

            local_id = local.get("id")
            local_nombre = local.get("name", "Local")
            visitante_id = visitante.get("id")
            visitante_nombre = visitante.get("name", "Visitante")

            if not local_id or not visitante_id:
                continue

            local_matches = fd_obtener_partidos_equipo(local_id, cantidad)
            visitante_matches = fd_obtener_partidos_equipo(visitante_id, cantidad)

            local_partidos = fd_convertir_matches_a_partidos(local_matches, local_nombre)
            visitante_partidos = fd_convertir_matches_a_partidos(visitante_matches, visitante_nombre)

            perfil_local = resumir_perfil_equipo(local_partidos)
            perfil_visitante = resumir_perfil_equipo(visitante_partidos)

            if not perfil_local or not perfil_visitante:
                continue

            eventos = combinar_items_partido(
                local_nombre,
                visitante_nombre,
                perfil_local,
                perfil_visitante
            )

            fuertes = [e for e in eventos if e["valor"] >= umbral]
            mejor_evento = eventos[0] if eventos else None

            partidos_hoy_lista.append({
                "partido": f"{local_nombre} vs {visitante_nombre}",
                "utcDate": f.get("utcDate", ""),
                "local": local_nombre,
                "visitante": visitante_nombre,
                "mejor_evento": mejor_evento,
                "eventos_fuertes": fuertes,
                "todos_los_eventos": eventos,
                "perfil_local": perfil_local,
                "perfil_visitante": perfil_visitante,
                "cantidad": cantidad
            })

        partidos_hoy_lista.sort(
            key=lambda x: x["mejor_evento"]["valor"] if x["mejor_evento"] else 0,
            reverse=True
        )

        return render_template(
            "partidos_hoy.html",
            league_name=league_name,
            season=season_actual,
            fecha_label=rango_fechas["label"],
            cantidad=cantidad,
            umbral=umbral,
            partidos=partidos_hoy_lista,
            scanned_matches=len(fixtures_a_escanear),
            max_matches=MAX_FIXTURES_TODAY_SCAN,
            error=None
        )

    except requests.HTTPError as e:
        return render_template(
            "partidos_hoy.html",
            league_name="Error",
            season="-",
            fecha_label="-",
            cantidad=0,
            umbral=umbral,
            partidos=[],
            scanned_matches=0,
            max_matches=MAX_FIXTURES_TODAY_SCAN,
            error=f"Error HTTP al consultar la API: {e}"
        )
    except requests.RequestException as e:
        return render_template(
            "partidos_hoy.html",
            league_name="Error",
            season="-",
            fecha_label="-",
            cantidad=0,
            umbral=umbral,
            partidos=[],
            scanned_matches=0,
            max_matches=MAX_FIXTURES_TODAY_SCAN,
            error=f"Error de conexión con la API: {e}"
        )
    except Exception as e:
        return render_template(
            "partidos_hoy.html",
            league_name="Error",
            season="-",
            fecha_label="-",
            cantidad=0,
            umbral=umbral,
            partidos=[],
            scanned_matches=0,
            max_matches=MAX_FIXTURES_TODAY_SCAN,
            error=f"Ocurrió un error inesperado: {e}"
        )

init_cache_db()

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)