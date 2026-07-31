
from flask import Flask, render_template, request, jsonify
import requests, re, sqlite3, json, os
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus
from datetime import datetime, timedelta

app = Flask(__name__)
BASE = "https://www.studbook.org.ar"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LEA-WIN-IA/1.0)",
    "Accept-Language": "es-AR,es;q=0.9"
}
DB = os.getenv("LEA_DB", "lea_win.db")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
SYNC_TOKEN = os.getenv("LEA_SYNC_TOKEN", "")
DEFAULT_USER = os.getenv("LEA_DEFAULT_USER", "usuario-local")

def clean(v):
    return re.sub(r"\s+", " ", v or "").strip()

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=(4, 8))
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def ensure_column(con, table, column, definition):
    columns = {
        row["name"]
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        con.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS carreras(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fecha TEXT NOT NULL, hipodromo TEXT NOT NULL, numero INTEGER NOT NULL,
      premio TEXT, distancia INTEGER, superficie TEXT, estado_publicado TEXT,
      condicion TEXT, pista_dia TEXT, clima TEXT, viento TEXT, retiros TEXT,
      observaciones TEXT, participantes TEXT NOT NULL, analisis TEXT,
      resultado_real TEXT, creado_en TEXT NOT NULL,
      UNIQUE(fecha,hipodromo,numero)
    );

    CREATE TABLE IF NOT EXISTS analisis_usuarios(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      carrera_id INTEGER NOT NULL,
      usuario_id TEXT NOT NULL,
      datos_manuales TEXT NOT NULL,
      analisis TEXT NOT NULL,
      creado_en TEXT NOT NULL,
      actualizado_en TEXT NOT NULL,
      UNIQUE(carrera_id,usuario_id),
      FOREIGN KEY(carrera_id) REFERENCES carreras(id)
    );
    """)
    ensure_column(con, "carreras", "comparacion", "TEXT")
    ensure_column(con, "carreras", "analisis_generado_en", "TEXT")
    ensure_column(con, "carreras", "resultado_actualizado_en", "TEXT")
    ensure_column(con, "carreras", "actualizado_en", "TEXT")
    con.commit()
    con.close()

def extract_races_from_meeting(soup):
    races = []
    for h in soup.find_all(["h2","h3"]):
        m = re.search(r"(\d+)\s*[º°ª]?\s*Carrera\b", clean(h.get_text(" ")), re.I)
        if m:
            races.append({"numero": int(m.group(1)), "titulo": clean(h.get_text(" "))})
    return races

def parse_official_result(nodes):
    """Extrae el resultado oficial de la tabla RESULTADOS de Stud Book."""
    tables, seen = [], set()
    for node in nodes:
        if getattr(node, "name", None) != "table":
            continue
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        tables.append(node)

    for table in tables:
        header_row = table.find("tr")
        if not header_row:
            continue

        headers = [
            clean(cell.get_text(" ")).lower()
            for cell in header_row.find_all(["th", "td"])
        ]
        if not headers or not any("ejemplar" in h for h in headers):
            continue

        def find_index(options, contains=False):
            for index, header in enumerate(headers):
                for option in options:
                    if (contains and option in header) or header == option:
                        return index
            return None

        position_index = find_index(
            ["p", "pos", "puesto", "posición", "posicion"]
        )
        order_index = find_index(
            ["o", "orden", "n", "nº", "n°", "numero", "número"]
        )
        horse_index = find_index(["ejemplar"], contains=True)

        if position_index is None or horse_index is None:
            continue

        result = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) <= max(position_index, horse_index):
                continue

            position_text = clean(
                cells[position_index].get_text(" ")
            )
            position_match = re.search(r"\d+", position_text)
            if not position_match:
                continue

            horse_name = clean(cells[horse_index].get_text(" "))
            horse_name = re.sub(r"^Image\s+", "", horse_name, flags=re.I)
            if not horse_name:
                continue

            order = None
            if order_index is not None and len(cells) > order_index:
                order_match = re.search(
                    r"\d+",
                    clean(cells[order_index].get_text(" ")),
                )
                if order_match:
                    order = int(order_match.group())

            result.append({
                "posicion": int(position_match.group()),
                "numero": order,
                "nombre": horse_name,
            })

        if result:
            result.sort(key=lambda item: item["posicion"])
            return result

    return []


def parse_race(soup, numero):
    heading = None
    pat = re.compile(rf"^{numero}\s*[º°ª]?\s*Carrera\b", re.I)
    for h in soup.find_all(["h2","h3"]):
        if pat.search(clean(h.get_text(" "))):
            heading = h
            break
    if not heading:
        return None

    nodes = []
    for node in heading.find_all_next():
        if node is not heading and node.name in ["h2","h3"] and re.search(
            r"\d+\s*[º°ª]?\s*Carrera\b", clean(node.get_text(" ")), re.I
        ):
            break
        nodes.append(node)

    block = clean(" ".join(
        n.get_text(" ", strip=True) for n in nodes if hasattr(n, "get_text")
    ))

    def get(pattern):
        m = re.search(pattern, block, re.I)
        return clean(m.group(1)) if m else ""

    participants, seen = [], set()
    for a in nodes:
        if getattr(a, "name", None) != "a":
            continue
        href = a.get("href", "")
        name = clean(a.get_text(" "))
        if "/ejemplares/" not in href or not name or name in seen:
            continue
        seen.add(name)
        context = clean(a.parent.get_text(" ", strip=True)) if a.parent else name
        n = re.search(r"(?:^|\s)(\d{1,2})\s+" + re.escape(name), context, re.I)
        kg = re.findall(r"\b(\d{2}(?:[.,]\d)?)\b", context)
        participants.append({
            "numero": int(n.group(1)) if n else None,
            "nombre": name,
            "perfil": urljoin(BASE, href),
            "detalle": context[:700],
            "peso": kg[-1].replace(",", ".") if kg else "",
            "retirado": False
        })

    return {
        "carrera": numero,
        "premio": get(r"Premio:\s*(.+?)\s+Distancia:"),
        "distancia": get(r"Distancia:\s*(\d+)\s*mts"),
        "condicion": get(r"Condición:\s*(.+?)\s+Pista:"),
        "superficie": get(r"Pista:\s*(.+?)\s*\|\s*Estado:"),
        "estado": get(r"Estado:\s*(.+?)\s*\|\s*Categoria:"),
        "categoria": get(r"Categoria:\s*(.+?)(?:Premios|PROGRAMA|RESULTADOS|$)"),
        "participantes": participants,
        "resultado_oficial": parse_official_result(nodes),
    }

def enrich_horse(horse):
    profile = horse.get("perfil", "")
    if not profile:
        return horse
    try:
        soup = fetch(profile)
        text = clean(soup.get_text(" "))
        horse["sexo"] = (re.search(r"\b(Macho|Hembra)\b", text, re.I) or [None, ""])[1]
        horse["campana"] = clean((re.search(r"#?\s*CAMPAÑA\s*(.+?)(?:POR HIPODROMO|PEDIGREE|$)", text, re.I) or [None, ""])[1])[:1000]
        horse["actuaciones"] = []
        for tr in soup.find_all("tr"):
            row = clean(tr.get_text(" "))
            if re.search(r"\d{2}/\d{2}/\d{4}", row):
                horse["actuaciones"].append(row[:500])
        horse["actuaciones"] = horse["actuaciones"][:12]
    except Exception:
        horse.setdefault("sexo", "")
        horse.setdefault("campana", "")
        horse.setdefault("actuaciones", [])
    return horse

def score_horse(h, context):
    # Puntaje transparente. Solo usa datos detectados o cargados.
    score, reasons = 50.0, []
    acts = h.get("actuaciones", [])
    campaign = h.get("campana", "").lower()
    detail = h.get("detalle", "").lower()

    if acts:
        score += min(14, len(acts) * 1.2)
        reasons.append("tiene campaña reciente disponible")
    if "ganador" in campaign or "ganadora" in campaign:
        score += 8; reasons.append("registra victorias")
    if "debut" in campaign or not acts:
        score += 1
        reasons.append("debutante o historial limitado: se mantiene sin penalización fuerte")
    if any(x in campaign for x in ["palermo","san isidro","la plata"]):
        score += 4; reasons.append("experiencia en hipódromos principales")
    if h.get("peso"):
        try:
            kg = float(h["peso"])
            if kg <= 56: score += 3; reasons.append("peso competitivo")
        except: pass
    if context.get("pista_dia") in ["Pesada","Barrosa","Húmeda"] and any(
        x in (campaign+" "+detail) for x in ["pesada","barrosa","húmeda","humeda"]
    ):
        score += 7; reasons.append("antecedente compatible con la pista del día")
    return round(max(1, min(99, score)), 1), reasons


def build_analysis(participants, context, analysis_type="oficial"):
    horses = [
        dict(h)
        for h in participants
        if not h.get("retirado")
    ]
    if len(horses) < 2:
        raise ValueError(
            "Se necesitan al menos dos participantes confirmados."
        )

    ranked = []
    for horse in horses:
        score, reasons = score_horse(horse, context)
        ranked.append({
            **horse,
            "score": score,
            "motivos": reasons,
        })

    ranked.sort(
        key=lambda item: (
            item["score"],
            -(item.get("numero") or 999),
        ),
        reverse=True,
    )
    top = ranked[:4]
    total = sum(item["score"] for item in top) or 1

    for item in top:
        item["probabilidad_relativa"] = round(
            item["score"] / total * 100,
            1,
        )

    return {
        "tipo": analysis_type,
        "ranking": top,
        "confianza": round(top[0]["score"], 1),
        "generado_en": datetime.now().isoformat(timespec="seconds"),
    }


def result_key(item):
    number = item.get("numero")
    if number not in (None, ""):
        try:
            return ("numero", int(number))
        except (TypeError, ValueError):
            pass
    return ("nombre", normalize_text(item.get("nombre", "")))


def compare_analysis_with_result(analysis, official_result):
    ranking = (analysis or {}).get("ranking", [])
    predicted = ranking[:4]
    actual = sorted(
        official_result or [],
        key=lambda item: item.get("posicion", 999),
    )[:4]

    predicted_keys = [result_key(item) for item in predicted]
    actual_keys = [result_key(item) for item in actual]

    exact = sum(
        1
        for index, key in enumerate(predicted_keys)
        if index < len(actual_keys) and key == actual_keys[index]
    )
    top4_hits = len(set(predicted_keys) & set(actual_keys))
    winner_hit = bool(
        predicted_keys
        and actual_keys
        and predicted_keys[0] == actual_keys[0]
    )

    return {
        "acierto_ganador": winner_hit,
        "aciertos_posicion_exacta": exact,
        "aciertos_dentro_top4": top4_hits,
        "pronostico": predicted,
        "resultado": actual,
        "comparado_en": datetime.now().isoformat(timespec="seconds"),
    }


def json_load(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def official_analysis_exists(fecha, hipodromo, numero):
    con = db()
    row = con.execute(
        """
        SELECT analisis
        FROM carreras
        WHERE fecha=? AND hipodromo=? AND numero=?
        """,
        (fecha, hipodromo, int(numero)),
    ).fetchone()
    con.close()
    if not row:
        return False
    analysis = json_load(row["analisis"], {})
    return bool(analysis.get("ranking"))


def save_official_race(data, analysis=None, official_result=None):
    now = datetime.now().isoformat(timespec="seconds")
    analysis_json = (
        json.dumps(analysis, ensure_ascii=False)
        if analysis
        else ""
    )
    result_json = (
        json.dumps(official_result, ensure_ascii=False)
        if official_result
        else ""
    )

    con = db()
    con.execute(
        """
        INSERT INTO carreras(
          fecha,hipodromo,numero,premio,distancia,superficie,
          estado_publicado,condicion,pista_dia,clima,viento,retiros,
          observaciones,participantes,analisis,resultado_real,
          creado_en,analisis_generado_en,resultado_actualizado_en,
          actualizado_en
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fecha,hipodromo,numero) DO UPDATE SET
          premio=CASE WHEN excluded.premio<>'' THEN excluded.premio
                      ELSE carreras.premio END,
          distancia=COALESCE(excluded.distancia,carreras.distancia),
          superficie=CASE WHEN excluded.superficie<>'' THEN excluded.superficie
                          ELSE carreras.superficie END,
          estado_publicado=CASE WHEN excluded.estado_publicado<>'' THEN excluded.estado_publicado
                                ELSE carreras.estado_publicado END,
          condicion=CASE WHEN excluded.condicion<>'' THEN excluded.condicion
                         ELSE carreras.condicion END,
          participantes=CASE WHEN excluded.participantes<>'[]' THEN excluded.participantes
                             ELSE carreras.participantes END,
          analisis=CASE
            WHEN carreras.analisis IS NULL
              OR trim(carreras.analisis) IN ('','{}','[]','null')
            THEN excluded.analisis
            ELSE carreras.analisis
          END,
          resultado_real=CASE
            WHEN excluded.resultado_real<>''
            THEN excluded.resultado_real
            ELSE carreras.resultado_real
          END,
          analisis_generado_en=CASE
            WHEN carreras.analisis IS NULL
              OR trim(carreras.analisis) IN ('','{}','[]','null')
            THEN excluded.analisis_generado_en
            ELSE carreras.analisis_generado_en
          END,
          resultado_actualizado_en=CASE
            WHEN excluded.resultado_real<>''
            THEN excluded.resultado_actualizado_en
            ELSE carreras.resultado_actualizado_en
          END,
          actualizado_en=excluded.actualizado_en
        """,
        (
            data["fecha"],
            data["hipodromo"],
            int(data["numero"]),
            data.get("premio", ""),
            data.get("distancia") or None,
            data.get("superficie", ""),
            data.get("estado_publicado", ""),
            data.get("condicion", ""),
            data.get("pista_dia", ""),
            data.get("clima", ""),
            data.get("viento", ""),
            json.dumps(data.get("retiros", []), ensure_ascii=False),
            data.get("observaciones", ""),
            json.dumps(
                data.get("participantes", []),
                ensure_ascii=False,
            ),
            analysis_json,
            result_json,
            now,
            now if analysis else None,
            now if official_result else None,
            now,
        ),
    )
    con.commit()

    row = con.execute(
        """
        SELECT id,analisis,resultado_real
        FROM carreras
        WHERE fecha=? AND hipodromo=? AND numero=?
        """,
        (
            data["fecha"],
            data["hipodromo"],
            int(data["numero"]),
        ),
    ).fetchone()

    stored_analysis = json_load(row["analisis"], {})
    stored_result = json_load(row["resultado_real"], [])
    comparison = None
    if stored_analysis.get("ranking") and stored_result:
        comparison = compare_analysis_with_result(
            stored_analysis,
            stored_result,
        )
        con.execute(
            """
            UPDATE carreras
            SET comparacion=?, actualizado_en=?
            WHERE id=?
            """,
            (
                json.dumps(comparison, ensure_ascii=False),
                now,
                row["id"],
            ),
        )
        con.commit()

    con.close()
    return comparison


def save_user_analysis(data):
    now = datetime.now().isoformat(timespec="seconds")
    user_id = clean(
        data.get("usuario_id")
        or request.headers.get("X-LEA-Usuario")
        or DEFAULT_USER
    )[:80]

    save_official_race(data)

    con = db()
    race = con.execute(
        """
        SELECT id
        FROM carreras
        WHERE fecha=? AND hipodromo=? AND numero=?
        """,
        (
            data["fecha"],
            data["hipodromo"],
            int(data["numero"]),
        ),
    ).fetchone()

    manual_data = {
        "pista_dia": data.get("pista_dia", ""),
        "clima": data.get("clima", ""),
        "viento": data.get("viento", ""),
        "retiros": data.get("retiros", []),
        "observaciones": data.get("observaciones", ""),
        "participantes": data.get("participantes", []),
    }

    con.execute(
        """
        INSERT INTO analisis_usuarios(
          carrera_id,usuario_id,datos_manuales,analisis,
          creado_en,actualizado_en
        )
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(carrera_id,usuario_id) DO UPDATE SET
          datos_manuales=excluded.datos_manuales,
          analisis=excluded.analisis,
          actualizado_en=excluded.actualizado_en
        """,
        (
            race["id"],
            user_id,
            json.dumps(manual_data, ensure_ascii=False),
            json.dumps(data.get("analisis", {}), ensure_ascii=False),
            now,
            now,
        ),
    )
    con.commit()
    con.close()
    return user_id


def normalize_text(value):
    value = clean(value).lower()
    for source, target in {
        "á": "a", "é": "e", "í": "i",
        "ó": "o", "ú": "u", "ü": "u",
    }.items():
        value = value.replace(source, target)
    return value


def meeting_date_from_url(url):
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", url or "")
    if not match:
        return ""
    try:
        return datetime.strptime(
            match.group(1),
            "%Y%m%d",
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def calendar_from_meetings(soup):
    meetings = []
    seen = set()

    for link in soup.select('a[href*="/reuniones/detalle/"]'):
        href = urljoin(BASE, link.get("href", ""))
        date = meeting_date_from_url(href)
        racecourse = clean(link.get_text(" ")) or "Hipódromo"

        if not date:
            continue

        key = (normalize_text(racecourse), date, href)
        if key in seen:
            continue
        seen.add(key)

        meetings.append({
            "hipodromo": racecourse,
            "fecha": date,
            "url": href,
        })

    meetings.sort(
        key=lambda item: (
            item["fecha"],
            normalize_text(item["hipodromo"]),
        )
    )
    return meetings


def saved_calendar():
    con = db()
    rows = con.execute(
        """
        SELECT DISTINCT fecha, hipodromo
        FROM carreras
        ORDER BY fecha DESC, hipodromo
        """
    ).fetchall()
    con.close()
    return [
        {
            "fecha": row["fecha"],
            "hipodromo": row["hipodromo"],
            "url": "",
        }
        for row in rows
    ]


@app.get("/api/calendario")
def calendario():
    try:
        meetings = calendar_from_meetings(
            fetch(BASE + "/reuniones")
        )
        if meetings:
            return jsonify(
                ok=True,
                reuniones=meetings,
                fuente="Stud Book",
            )
    except Exception:
        pass

    saved = saved_calendar()
    if saved:
        return jsonify(
            ok=True,
            reuniones=saved,
            fuente="Carreras guardadas",
            aviso=(
                "La fuente oficial no respondió. "
                "Se muestran fechas guardadas."
            ),
        )

    return jsonify(
        ok=False,
        error=(
            "El calendario oficial no está disponible "
            "en este momento."
        ),
        reuniones=[],
    ), 503


@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/reuniones")
def reuniones():
    fecha = request.args.get("fecha", "").strip()
    hipodromo = request.args.get("hipodromo", "").strip()

    if not fecha or not hipodromo:
        return jsonify(
            ok=False,
            error="Elegí hipódromo y fecha.",
        ), 400

    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return jsonify(ok=False, error="Fecha inválida."), 400

    try:
        calendar = calendar_from_meetings(
            fetch(BASE + "/reuniones")
        )
        selected = [
            meeting
            for meeting in calendar
            if meeting["fecha"] == fecha
            and normalize_text(meeting["hipodromo"])
            == normalize_text(hipodromo)
        ]

        output = []
        for meeting in selected:
            detail = fetch(meeting["url"])
            races = extract_races_from_meeting(detail)
            if races:
                output.append({
                    "hipodromo": meeting["hipodromo"],
                    "url": meeting["url"],
                    "carreras": races,
                })

        if not output:
            return jsonify(
                ok=False,
                error=(
                    "No se encontraron carreras confirmadas "
                    "para esa reunión."
                ),
                reuniones=[],
            ), 404

        return jsonify(ok=True, reuniones=output)

    except Exception:
        return jsonify(
            ok=False,
            error=(
                "La fuente oficial no respondió. "
                "Probá nuevamente más tarde."
            ),
            reuniones=[],
        ), 503


@app.get("/api/carrera")
def carrera():
    url = request.args.get("url","")
    numero = request.args.get("numero","")
    if not url.startswith(BASE) or not numero.isdigit():
        return jsonify(ok=False,error="Datos inválidos."),400
    try:
        data = parse_race(fetch(url), int(numero))
        if not data:
            return jsonify(ok=False,error="No se encontró la carrera."),404
        return jsonify(ok=True, **data)
    except Exception as e:
        return jsonify(ok=False,error="No se pudo cargar la carrera.",detalle=str(e)),502

@app.post("/api/enriquecer")
def enriquecer():
    data = request.get_json(silent=True) or {}
    horses = data.get("participantes", [])
    return jsonify(ok=True,participantes=[enrich_horse(dict(h)) for h in horses])

@app.post("/api/analizar")
def analizar():
    data = request.get_json(silent=True) or {}
    try:
        analysis = build_analysis(
            data.get("participantes", []),
            data,
            analysis_type="usuario",
        )
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400

    return jsonify(ok=True, **analysis)

@app.get("/api/videos")
def videos():
    horse = request.args.get("caballo","").strip()
    if not horse:
        return jsonify(ok=False,error="Falta el caballo."),400
    query = f'{horse} carrera caballo Argentina'
    if not YOUTUBE_API_KEY:
        return jsonify(ok=True,modo="busqueda",url="https://www.youtube.com/results?search_query="+quote_plus(query),videos=[])
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part":"snippet","q":query,"type":"video","maxResults":5,"key":YOUTUBE_API_KEY}
    r = requests.get(url,params=params,timeout=20)
    r.raise_for_status()
    items = [{
        "id":x["id"]["videoId"],
        "titulo":x["snippet"]["title"],
        "miniatura":x["snippet"]["thumbnails"]["medium"]["url"]
    } for x in r.json().get("items",[])]
    return jsonify(ok=True,modo="api",videos=items)

@app.post("/api/guardar")
def guardar():
    data = request.get_json(silent=True) or {}
    if not all(
        data.get(key)
        for key in ["fecha", "hipodromo", "numero", "participantes"]
    ):
        return jsonify(ok=False, error="Faltan datos."), 400

    user_id = save_user_analysis(data)
    return jsonify(
        ok=True,
        usuario_id=user_id,
        mensaje=(
            "Análisis ajustado del usuario guardado por separado. "
            "El análisis oficial no fue reemplazado."
        ),
    )

@app.get("/api/historial")
def historial():
    user_id = clean(
        request.args.get("usuario_id")
        or request.headers.get("X-LEA-Usuario")
        or DEFAULT_USER
    )[:80]

    con = db()
    rows = con.execute(
        """
        SELECT
          c.id,c.fecha,c.hipodromo,c.numero,c.premio,
          c.pista_dia,c.clima,c.analisis,c.resultado_real,
          c.comparacion,c.analisis_generado_en,
          u.analisis AS analisis_usuario,
          u.datos_manuales AS datos_manuales_usuario
        FROM carreras c
        LEFT JOIN analisis_usuarios u
          ON u.carrera_id=c.id AND u.usuario_id=?
        ORDER BY c.fecha DESC,c.numero
        """,
        (user_id,),
    ).fetchall()
    con.close()

    races = []
    for row in rows:
        item = dict(row)
        item["analisis"] = json_load(item["analisis"], {})
        item["resultado_real"] = json_load(
            item["resultado_real"],
            [],
        )
        item["comparacion"] = json_load(
            item["comparacion"],
            {},
        )
        item["analisis_usuario"] = json_load(
            item["analisis_usuario"],
            {},
        )
        item["datos_manuales_usuario"] = json_load(
            item["datos_manuales_usuario"],
            {},
        )
        races.append(item)

    return jsonify(
        ok=True,
        usuario_id=user_id,
        carreras=races,
    )


def valid_sync_request():
    if not SYNC_TOKEN:
        return True
    supplied = (
        request.headers.get("X-LEA-Sync-Token")
        or request.args.get("token", "")
    )
    return supplied == SYNC_TOKEN


def sync_studbook(date_from=None, date_to=None, max_new=2):
    today = datetime.now().date()
    start = date_from or (today - timedelta(days=2))
    end = date_to or (today + timedelta(days=45))

    meetings = calendar_from_meetings(
        fetch(BASE + "/reuniones")
    )
    selected = []
    for meeting in meetings:
        try:
            meeting_date = datetime.strptime(
                meeting["fecha"],
                "%Y-%m-%d",
            ).date()
        except ValueError:
            continue
        if start <= meeting_date <= end:
            selected.append(meeting)

    summary = {
        "reuniones_revisadas": 0,
        "carreras_detectadas": 0,
        "analisis_nuevos": 0,
        "resultados_nuevos": 0,
        "comparaciones": 0,
        "errores": [],
    }

    for meeting in selected:
        try:
            soup = fetch(meeting["url"])
            races = extract_races_from_meeting(soup)
            summary["reuniones_revisadas"] += 1

            for race in races:
                parsed = parse_race(soup, race["numero"])
                if not parsed:
                    continue

                summary["carreras_detectadas"] += 1
                official_result = parsed.get(
                    "resultado_oficial",
                    [],
                )

                race_data = {
                    "fecha": meeting["fecha"],
                    "hipodromo": meeting["hipodromo"],
                    "numero": parsed["carrera"],
                    "premio": parsed.get("premio", ""),
                    "distancia": parsed.get("distancia") or None,
                    "superficie": parsed.get("superficie", ""),
                    "estado_publicado": parsed.get("estado", ""),
                    "condicion": parsed.get("condicion", ""),
                    "participantes": parsed.get("participantes", []),
                }

                analysis = None
                has_analysis = official_analysis_exists(
                    race_data["fecha"],
                    race_data["hipodromo"],
                    race_data["numero"],
                )

                if (
                    not has_analysis
                    and summary["analisis_nuevos"] < max_new
                    and len(race_data["participantes"]) >= 2
                ):
                    enriched = [
                        enrich_horse(dict(horse))
                        for horse in race_data["participantes"]
                    ]
                    race_data["participantes"] = enriched
                    analysis = build_analysis(
                        enriched,
                        race_data,
                        analysis_type="oficial",
                    )
                    summary["analisis_nuevos"] += 1

                comparison = save_official_race(
                    race_data,
                    analysis=analysis,
                    official_result=official_result,
                )

                if official_result:
                    summary["resultados_nuevos"] += 1
                if comparison:
                    summary["comparaciones"] += 1

        except Exception as error:
            summary["errores"].append({
                "reunion": meeting.get("url", ""),
                "detalle": str(error),
            })

    return summary


@app.post("/api/sincronizar-studbook")
def sincronizar_studbook():
    if not valid_sync_request():
        return jsonify(ok=False, error="No autorizado."), 401

    data = request.get_json(silent=True) or {}

    def parse_date(value):
        if not value:
            return None
        return datetime.strptime(value, "%Y-%m-%d").date()

    try:
        date_from = parse_date(data.get("fecha_desde"))
        date_to = parse_date(data.get("fecha_hasta"))
        max_new = max(1, min(5, int(data.get("max_nuevas", 2))))
    except (ValueError, TypeError):
        return jsonify(
            ok=False,
            error="Parámetros de sincronización inválidos.",
        ), 400

    try:
        summary = sync_studbook(
            date_from=date_from,
            date_to=date_to,
            max_new=max_new,
        )
        return jsonify(
            ok=True,
            mensaje=(
                "Stud Book revisado. Las carreras nuevas se analizaron "
                "sin reemplazar los análisis oficiales existentes."
            ),
            resumen=summary,
        )
    except Exception as error:
        return jsonify(
            ok=False,
            error="No se pudo sincronizar Stud Book.",
            detalle=str(error),
        ), 502


@app.get("/api/estado-automatizacion")
def estado_automatizacion():
    con = db()
    row = con.execute(
        """
        SELECT
          COUNT(*) AS carreras,
          SUM(CASE WHEN analisis IS NOT NULL
                    AND trim(analisis) NOT IN ('','{}','[]','null')
                   THEN 1 ELSE 0 END) AS analizadas,
          SUM(CASE WHEN resultado_real IS NOT NULL
                    AND trim(resultado_real) NOT IN ('','[]','null')
                   THEN 1 ELSE 0 END) AS con_resultado,
          SUM(CASE WHEN comparacion IS NOT NULL
                    AND trim(comparacion) NOT IN ('','{}','null')
                   THEN 1 ELSE 0 END) AS comparadas
        FROM carreras
        """
    ).fetchone()
    con.close()
    return jsonify(ok=True, estado=dict(row))

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=True)
