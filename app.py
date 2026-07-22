
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

    def get(pattern):
        m = re.search(pattern, block, re.I)
        return clean(m.group(1)) if m else ""

    participants, seen = [], set()

    def participant_context(anchor, name):
        """
        Sube por los contenedores hasta encontrar la fila completa del ejemplar.
        El participante real aparece como: NOMBRE por PADRE y MADRE.
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
                return text

        return ""

    for a in nodes:
        if getattr(a, "name", None) != "a":
            continue

        href = a.get("href", "")
        name = clean(a.get_text(" "))

        if "/ejemplares/" not in href or not name or name in seen:
            continue

        context = participant_context(a, name)

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
