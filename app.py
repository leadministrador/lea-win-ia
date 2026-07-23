
from flask import Flask, render_template, request, jsonify
import requests, re, sqlite3, json, os
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus
from datetime import datetime

app = Flask(__name__)
BASE = "https://www.studbook.org.ar"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
    "Referer": "https://www.studbook.org.ar/",
    "Connection": "keep-alive"
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
DB = os.getenv("LEA_DB", "lea_win.db")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

def clean(v):
    return re.sub(r"\s+", " ", v or "").strip()

def fetch_response(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r

def fetch(url):
    return BeautifulSoup(fetch_response(url).text, "html.parser")

def meeting_urls_from_html(html, date_key):
    """Extrae enlaces oficiales incluso si están dentro de scripts o JSON."""
    found = set()
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.select('a[href*="/reuniones/detalle/"]'):
        href = urljoin(BASE, a.get("href", ""))
        if date_key in href:
            found.add(href)

    pattern = rf'(?P<url>(?:https://www\.studbook\.org\.ar)?/reuniones/detalle/\d+/{date_key}-[^"\'<>\\\s]+)'
    for match in re.finditer(pattern, html, re.I):
        found.add(urljoin(BASE, match.group("url")))

    return sorted(found)

def locate_meetings(date_value):
    """Busca reuniones por calendario, portada y un localizador de respaldo."""
    dt = datetime.strptime(date_value, "%Y-%m-%d")
    date_key = dt.strftime("%Y%m%d")
    urls = set()

    # 1. Calendario oficial del mes elegido.
    calendar_url = f"{BASE}/reuniones?anio={dt.year}&mes={dt.month}"
    calendar_html = fetch_response(calendar_url).text
    urls.update(meeting_urls_from_html(calendar_html, date_key))

    # 2. Portada oficial, útil para las reuniones del día.
    if not urls:
        home_html = fetch_response(BASE + "/").text
        urls.update(meeting_urls_from_html(home_html, date_key))

    # 3. Respaldo: el buscador solo localiza el enlace; los datos se leen
    # siempre desde la página oficial de Stud Book.
    if not urls:
        query = quote_plus(
            f"site:studbook.org.ar/reuniones/detalle {date_key}"
        )
        rss_url = f"https://www.bing.com/search?format=rss&q={query}"
        try:
            rss = SESSION.get(rss_url, timeout=20)
            rss.raise_for_status()
            rss_soup = BeautifulSoup(rss.text, "xml")
            for item in rss_soup.find_all("item"):
                link = clean(item.link.get_text()) if item.link else ""
                if (
                    "studbook.org.ar/reuniones/detalle/" in link
                    and date_key in link
                ):
                    urls.add(link)
        except requests.RequestException:
            pass

    return sorted(urls)

def meeting_name(soup, fallback="Hipódromo"):
    text = clean(soup.get_text(" "))
    match = re.search(
        r"\d{2}/\d{2}/\d{4}\s+Hipodromo de\s+(.+?)\s*\|",
        text,
        re.I
    )
    return clean(match.group(1)) if match else fallback

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

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
    """)
    con.commit()
    con.close()

def extract_races_from_meeting(soup):
    races = []
    for h in soup.find_all(["h2","h3"]):
        m = re.search(r"(\d+)\s*[º°ª]?\s*Carrera\b", clean(h.get_text(" ")), re.I)
        if m:
            races.append({"numero": int(m.group(1)), "titulo": clean(h.get_text(" "))})
    return races

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
    page_text = clean(soup.get_text(" "))
    race_date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", page_text)
    race_date = race_date_match.group(1) if race_date_match else ""

    def get(pattern):
        m = re.search(pattern, block, re.I)
        return clean(m.group(1)) if m else ""

    participants, seen = [], set()

    def participant_context(anchor, name):
        """
        Devuelve el contenedor más pequeño de la fila del participante
        y su texto. Los enlaces de resultados dentro de esa fila apuntan
        a las carreras anteriores oficiales en Stud Book.
        """
        for parent in anchor.parents:
            if getattr(parent, "name", None) not in {
                "tr", "li", "article", "section", "div"
            }:
                continue

            text = clean(parent.get_text(" ", strip=True))
            if re.search(
                re.escape(name) + r"\s+por\b",
                text,
                re.I
            ):
                return parent, text

        return None, ""

    for a in nodes:
        if getattr(a, "name", None) != "a":
            continue

        href = a.get("href", "")
        name = clean(a.get_text(" "))

        if "/ejemplares/" not in href or not name or name in seen:
            continue

        row_node, context = participant_context(a, name)

        # Excluye padre, madre y demás enlaces de la fila.
        # Solo el caballo participante está seguido por la palabra "por".
        if not context or not re.search(
            re.escape(name) + r"\s+por\b",
            context,
            re.I
        ):
            continue

        number_match = re.search(
            r"(?:^|\s)(\d{1,2})\s+(?:Image\s+)?"
            + re.escape(name)
            + r"\s+por\b",
            context,
            re.I
        )

        # Sin número confirmado no se incorpora el enlace como participante.
        if not number_match:
            continue

        number = int(number_match.group(1))
        unique_key = (number, name.upper())
        if unique_key in seen:
            continue
        seen.add(unique_key)

        pedigree_match = re.search(
            re.escape(name)
            + r"\s+por\s+(.+?)\s+y\s+(.+?)"
            + r"\s+([MH])\s+(\S+)\s+(\d+)\b",
            context,
            re.I
        )

        weight_match = re.search(
            r"\b((?:4[8-9]|5\d|6[0-5])(?:[.,]\d)?)\b",
            context
        )

        campaign_matches = re.findall(
            r"\b\d+\s*-\s*\d+\s*-\s*\d+\s*-\s*"
            r"\d+\s*-\s*\d+\s*-\s*\d+\s*-\s*\d+\b",
            context
        )

        def linked_person(fragment):
            if row_node is None:
                return "", ""
            link = row_node.select_one(f'a[href*="{fragment}"]')
            if not link:
                return "", ""
            return clean(link.get_text(" ")), urljoin(BASE, link.get("href", ""))

        jockey, jockey_url = linked_person("/profesionales/jockey/")
        entrenador, entrenador_url = linked_person("/profesionales/entrenador/")
        caballeriza, caballeriza_url = linked_person("/caballerizas/perfil/")

        peso_caballo = ""
        peso_jockey = ""

        cells = []
        if row_node is not None and getattr(row_node, "name", None) == "tr":
            cells = row_node.find_all(["td", "th"], recursive=False)

        jockey_link = (
            row_node.select_one('a[href*="/profesionales/jockey/"]')
            if row_node is not None else None
        )

        if cells and jockey_link:
            jockey_cell = jockey_link.find_parent(["td", "th"])
            if jockey_cell in cells:
                jockey_index = cells.index(jockey_cell)

                # El peso corporal está antes de la celda del jockey.
                for cell in reversed(cells[:jockey_index]):
                    value = clean(cell.get_text(" "))
                    match = re.fullmatch(r"(\d{3})", value)
                    if match and 300 <= int(match.group(1)) <= 700:
                        peso_caballo = match.group(1)
                        break

                # El peso que carga está después del jockey.
                for cell in cells[jockey_index + 1:jockey_index + 3]:
                    value = clean(cell.get_text(" "))
                    match = re.fullmatch(r"(\d{2}(?:[.,]\d)?)", value)
                    if match:
                        number_value = float(match.group(1).replace(",", "."))
                        if 45 <= number_value <= 65:
                            peso_jockey = match.group(1).replace(",", ".")
                            break

        # Respaldo para formatos donde la fila no usa celdas HTML.
        if jockey:
            before_jockey = context.split(jockey, 1)[0]
            if not peso_caballo:
                body_candidates = re.findall(r"\b(\d{3})\b", before_jockey)
                valid_body = [
                    value for value in body_candidates
                    if 300 <= int(value) <= 700
                ]
                if valid_body:
                    peso_caballo = valid_body[-1]

            if not peso_jockey:
                after_jockey = context.split(jockey, 1)[1]
                if entrenador and entrenador in after_jockey:
                    after_jockey = after_jockey.split(entrenador, 1)[0]
                load_candidates = re.findall(
                    r"\b(\d{2}(?:[.,]\d)?)\b",
                    after_jockey
                )
                for value in load_candidates:
                    number_value = float(value.replace(",", "."))
                    if 45 <= number_value <= 65:
                        peso_jockey = value.replace(",", ".")
                        break

        previous_races = []
        if row_node is not None:
            for race_link in row_node.select(
                'a[href*="/reuniones/carrera/"]'
            ):
                race_url = urljoin(BASE, race_link.get("href", ""))
                result_code = clean(race_link.get_text(" "))
                if race_url and not any(
                    item["url"] == race_url for item in previous_races
                ):
                    previous_races.append({
                        "codigo": result_code,
                        "url": race_url
                    })

        participants.append({
            "numero": number,
            "nombre": name,
            "perfil": urljoin(BASE, href),
            "detalle": context[:900],
            "peso": (
                weight_match.group(1).replace(",", ".")
                if weight_match else ""
            ),
            "padre": (
                clean(pedigree_match.group(1))
                if pedigree_match else ""
            ),
            "madre": (
                clean(pedigree_match.group(2))
                if pedigree_match else ""
            ),
            "sexo": (
                pedigree_match.group(3).upper()
                if pedigree_match else ""
            ),
            "pelaje": (
                pedigree_match.group(4).upper()
                if pedigree_match else ""
            ),
            "edad": (
                int(pedigree_match.group(5))
                if pedigree_match else None
            ),
            "campana_resumen": (
                campaign_matches[-1]
                if campaign_matches else ""
            ),
            "jockey": jockey,
            "jockey_url": jockey_url,
            "peso_jockey": peso_jockey,
            "peso": peso_jockey,
            "entrenador": entrenador,
            "entrenador_url": entrenador_url,
            "caballeriza": caballeriza,
            "caballeriza_url": caballeriza_url,
            "peso_caballo": peso_caballo,
            "fecha_carrera": race_date,
            "carreras_previas": previous_races,
            "ultimas_8": previous_races[:8],
            "retirado": False
        })

    participants.sort(
        key=lambda item: (
            item["numero"] is None,
            item["numero"] if item["numero"] is not None else 999
        )
    )

    return {
        "carrera": numero,
        "premio": get(r"Premio:\s*(.+?)\s+Distancia:"),
        "distancia": get(r"Distancia:\s*(\d+)\s*mts"),
        "condicion": get(r"Condición:\s*(.+?)\s+Pista:"),
        "superficie": get(r"Pista:\s*(.+?)\s*\|\s*Estado:"),
        "estado": get(r"Estado:\s*(.+?)\s*\|\s*Categoria:"),
        "categoria": get(r"Categoria:\s*(.+?)(?:Premios|PROGRAMA|RESULTADOS|$)"),
        "participantes": participants
    }


def youtube_video_id(value):
    """Obtiene el identificador de un enlace real de YouTube."""
    if not value:
        return ""

    patterns = [
        r"(?:youtube\.com|youtube-nocookie\.com)/(?:embed|shorts|live)/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/watch\?(?:[^#\s]*&)?v=([A-Za-z0-9_-]{6,})",
        r"youtu\.be/([A-Za-z0-9_-]{6,})"
    ]

    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return match.group(1)

    return ""


def extract_exact_videos(soup):
    """
    Busca únicamente videos enlazados dentro de la página oficial
    de esa carrera. No realiza búsquedas generales por nombre.
    """
    found = {}

    for tag in soup.find_all(True):
        for attribute in (
            "href", "src", "data-src", "data-url", "data-video",
            "data-youtube", "onclick"
        ):
            value = tag.get(attribute)
            if not isinstance(value, str):
                continue

            video_id = youtube_video_id(value)
            if video_id:
                found[video_id] = {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "embed": f"https://www.youtube.com/embed/{video_id}"
                }

    raw_html = str(soup)
    for pattern in [
        r"https?://(?:www\.)?(?:youtube\.com|youtube-nocookie\.com)/(?:embed|shorts|live)/[A-Za-z0-9_-]{6,}",
        r"https?://(?:www\.)?youtube\.com/watch\?[^\"'<>\s]+",
        r"https?://youtu\.be/[A-Za-z0-9_-]{6,}"
    ]:
        for value in re.findall(pattern, raw_html, re.I):
            video_id = youtube_video_id(value.replace("&amp;", "&"))
            if video_id:
                found[video_id] = {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "embed": f"https://www.youtube.com/embed/{video_id}"
                }

    return list(found.values())


def race_information(soup, race_url, horse_name="", result_code=""):
    text = clean(soup.get_text(" "))

    date_track = re.search(
        r"(\d{2}/\d{2}/\d{4})\s+(.+?)(?:PDF|Programa oficial|\d+[º°ª]\s*Carrera)",
        text,
        re.I
    )
    race_heading = next(
        (
            clean(h.get_text(" "))
            for h in soup.find_all(["h2", "h3"])
            if re.search(r"\d+\s*[º°ª]?\s*Carrera", clean(h.get_text(" ")), re.I)
        ),
        ""
    )
    prize = re.search(r"Premio:\s*(.+?)\s+Distancia:", text, re.I)
    distance = re.search(r"Distancia:\s*(\d+)\s*mts", text, re.I)
    track_state = re.search(
        r"Pista:\s*(.+?)(?:\s*\|\s*Categoria:|Premios|RESULTADOS)",
        text,
        re.I
    )

    position = ""
    if horse_name:
        for row in soup.find_all("tr"):
            row_text = clean(row.get_text(" "))
            if horse_name.lower() in row_text.lower():
                position_match = re.match(r"(\d{1,2})\s+\d{1,2}\s+", row_text)
                if position_match:
                    position = position_match.group(1)
                    break

    return {
        "codigo": result_code,
        "url_studbook": race_url,
        "fecha": date_track.group(1) if date_track else "",
        "hipodromo": clean(date_track.group(2)) if date_track else "",
        "carrera": race_heading,
        "premio": clean(prize.group(1)) if prize else "",
        "distancia": int(distance.group(1)) if distance else None,
        "pista": clean(track_state.group(1)) if track_state else "",
        "posicion": position,
        "videos": extract_exact_videos(soup)
    }


def horse_previous_races(horse, limit=12):
    profile = horse.get("perfil", "")
    horse_name = horse.get("nombre", "")
    links = []

    # Primero conserva las ocho actuaciones enlazadas en el programa.
    for item in horse.get("carreras_previas", []):
        url = item.get("url", "")
        if url.startswith(BASE + "/reuniones/carrera/"):
            links.append({
                "url": url,
                "codigo": item.get("codigo", "")
            })

    # Luego completa la campaña desde el perfil individual.
    if profile.startswith(BASE + "/ejemplares/"):
        try:
            profile_soup = fetch(profile)
            for anchor in profile_soup.select(
                'a[href*="/reuniones/carrera/"]'
            ):
                url = urljoin(BASE, anchor.get("href", ""))
                links.append({
                    "url": url,
                    "codigo": clean(anchor.get_text(" "))
                })
        except Exception:
            pass

    unique_links = []
    seen_urls = set()
    for item in links:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        unique_links.append(item)
        if len(unique_links) >= limit:
            break

    races = []
    for item in unique_links:
        try:
            race_soup = fetch(item["url"])
            races.append(
                race_information(
                    race_soup,
                    item["url"],
                    horse_name,
                    item.get("codigo", "")
                )
            )
        except Exception:
            races.append({
                "codigo": item.get("codigo", ""),
                "url_studbook": item["url"],
                "fecha": "",
                "hipodromo": "",
                "carrera": "",
                "premio": "",
                "distancia": None,
                "pista": "",
                "posicion": "",
                "videos": [],
                "error": "No se pudo leer esta carrera."
            })

    def race_sort_key(race):
        """
        Ordena primero las carreras con fecha válida y luego las más recientes.
        Las carreras sin fecha quedan al final.
        """
        value = race.get("fecha", "")
        try:
            parsed = datetime.strptime(value, "%d/%m/%Y")
            return (1, parsed)
        except (TypeError, ValueError):
            return (0, datetime.min)

    races.sort(key=race_sort_key, reverse=True)
    return races


def enrich_horse(horse):
    profile = horse.get("perfil", "")
    if not profile:
        return horse

    try:
        soup = fetch(profile)
        text = clean(soup.get_text(" "))

        horse["sexo"] = (
            re.search(r"\b(Macho|Hembra)\b", text, re.I)
            or [None, horse.get("sexo", "")]
        )[1]

        horse["campana"] = clean((
            re.search(
                r"#?\s*CAMPAÑA\s*(.+?)(?:POR HIPODROMO|PEDIGREE|$)",
                text,
                re.I
            ) or [None, ""]
        )[1])[:1000]

        current_date = None
        if horse.get("fecha_carrera"):
            try:
                current_date = datetime.strptime(
                    horse["fecha_carrera"],
                    "%d/%m/%Y"
                )
            except ValueError:
                current_date = None

        performances = []
        for tr in soup.find_all("tr"):
            row = clean(tr.get_text(" "))
            date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", row)
            if not date_match:
                continue

            try:
                performance_date = datetime.strptime(
                    date_match.group(1),
                    "%d/%m/%Y"
                )
            except ValueError:
                continue

            # Excluye anotaciones futuras.
            if current_date and performance_date >= current_date:
                continue

            performances.append({
                "fecha": date_match.group(1),
                "fecha_orden": performance_date,
                "detalle": row[:500]
            })

        performances.sort(
            key=lambda item: item["fecha_orden"],
            reverse=True
        )

        latest_eight = performances[:8]
        horse["actuaciones_detalle"] = [
            {
                "fecha": item["fecha"],
                "detalle": item["detalle"]
            }
            for item in latest_eight
        ]
        horse["actuaciones"] = [
            item["detalle"] for item in latest_eight
        ]

        if latest_eight:
            horse["ultima_actuacion"] = latest_eight[0]["fecha"]

            if current_date:
                horse["dias_sin_correr"] = (
                    current_date - latest_eight[0]["fecha_orden"]
                ).days

    except Exception:
        horse.setdefault("sexo", "")
        horse.setdefault("campana", "")
        horse.setdefault("actuaciones", [])
        horse.setdefault("actuaciones_detalle", [])

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

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/reuniones")
def reuniones():
    fecha = request.args.get("fecha", "").strip()
    if not fecha:
        return jsonify(ok=False, error="Falta la fecha."), 400

    try:
        output = []
        for href in locate_meetings(fecha):
            detail = fetch(href)
            races = extract_races_from_meeting(detail)
            if not races:
                continue
            output.append({
                "hipodromo": meeting_name(detail),
                "url": href,
                "carreras": races
            })

        output.sort(key=lambda x: x["hipodromo"])
        return jsonify(ok=True, reuniones=output)

    except ValueError:
        return jsonify(ok=False, error="La fecha no es válida."), 400
    except Exception as e:
        return jsonify(
            ok=False,
            error="No se pudieron consultar las reuniones.",
            detalle=str(e)
        ), 502

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
    horses = [h for h in data.get("participantes",[]) if not h.get("retirado")]
    if len(horses) < 2:
        return jsonify(ok=False,error="Se necesitan al menos dos participantes confirmados."),400
    ranked = []
    for h in horses:
        score, reasons = score_horse(h, data)
        ranked.append({**h,"score":score,"motivos":reasons})
    ranked.sort(key=lambda x:x["score"], reverse=True)
    top = ranked[:4]
    total = sum(x["score"] for x in top) or 1
    for x in top:
        x["probabilidad_relativa"] = round(x["score"]/total*100,1)
    return jsonify(ok=True,ranking=top,confianza=round(top[0]["score"],1))

@app.post("/api/carreras-videos")
def carreras_videos():
    data = request.get_json(silent=True) or {}
    horse = data.get("caballo") or {}

    profile = horse.get("perfil", "")
    previous = horse.get("carreras_previas", [])

    if (
        not profile.startswith(BASE + "/ejemplares/")
        and not previous
    ):
        return jsonify(
            ok=False,
            error="Este participante no tiene enlaces oficiales disponibles."
        ), 400

    races = horse_previous_races(horse)
    videos_count = sum(len(race.get("videos", [])) for race in races)

    return jsonify(
        ok=True,
        caballo=horse.get("nombre", ""),
        carreras=races,
        total_carreras=len(races),
        total_videos=videos_count
    )


@app.post("/api/guardar")
def guardar():
    data = request.get_json(silent=True) or {}
    if not all(data.get(k) for k in ["fecha","hipodromo","numero","participantes"]):
        return jsonify(ok=False,error="Faltan datos."),400
    con = db()
    con.execute("""
    INSERT INTO carreras(fecha,hipodromo,numero,premio,distancia,superficie,
    estado_publicado,condicion,pista_dia,clima,viento,retiros,observaciones,
    participantes,analisis,resultado_real,creado_en)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(fecha,hipodromo,numero) DO UPDATE SET
    pista_dia=excluded.pista_dia,clima=excluded.clima,viento=excluded.viento,
    retiros=excluded.retiros,observaciones=excluded.observaciones,
    participantes=excluded.participantes,analisis=excluded.analisis,
    creado_en=excluded.creado_en
    """,(
      data["fecha"],data["hipodromo"],int(data["numero"]),data.get("premio",""),
      data.get("distancia"),data.get("superficie",""),data.get("estado_publicado",""),
      data.get("condicion",""),data.get("pista_dia",""),data.get("clima",""),
      data.get("viento",""),json.dumps(data.get("retiros",[]),ensure_ascii=False),
      data.get("observaciones",""),json.dumps(data["participantes"],ensure_ascii=False),
      json.dumps(data.get("analisis",{}),ensure_ascii=False),"",
      datetime.now().isoformat(timespec="seconds")
    ))
    con.commit(); con.close()
    return jsonify(ok=True,mensaje="Carrera y análisis guardados.")

@app.get("/api/historial")
def historial():
    con=db()
    rows=con.execute("""SELECT id,fecha,hipodromo,numero,premio,pista_dia,clima,
    analisis,resultado_real FROM carreras ORDER BY fecha DESC,numero""").fetchall()
    con.close()
    return jsonify(ok=True,carreras=[dict(x) for x in rows])

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=True)
