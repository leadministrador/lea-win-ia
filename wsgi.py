import json
from datetime import datetime

from flask import jsonify, request

from app import app, clean, db


AUTO_SAVE_SCRIPT = r'''
<script id="lea-auto-save-v1">
(function () {
  const botonManual = document.querySelector('#analisis button[onclick="guardar()"]');
  if (botonManual) botonManual.remove();

  window.analizar = async function () {
    const caja = document.getElementById('ranking');
    const body = datosCarrera();

    caja.textContent = 'Analizando y guardando...';
    go('analisis');

    try {
      const respuesta = await fetch('/api/analizar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      const datos = await respuesta.json();

      if (!datos.ok) {
        caja.textContent = datos.error || 'No se pudo analizar la carrera.';
        return;
      }

      ultimoAnalisis = datos;
      caja.innerHTML = datos.ranking.map((x, i) =>
        `<div class="rank"><b>${i + 1}.º · N.º ${x.numero || '?'} — ${x.nombre}</b><br>` +
        `Score: ${x.score}/100 · Probabilidad relativa: ${x.probabilidad_relativa}%` +
        `<div class="small">${(x.motivos || []).join(' · ') || 'Sin motivos suficientes detectados'}</div></div>`
      ).join('');

      const guardado = await fetch('/api/guardar', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...datosCarrera(), analisis: ultimoAnalisis})
      });
      const resultadoGuardado = await guardado.json();

      caja.innerHTML += `<div class="status">${
        resultadoGuardado.ok
          ? 'Análisis guardado automáticamente.'
          : (resultadoGuardado.error || 'El análisis se generó, pero no pudo guardarse.')
      }</div>`;
    } catch (error) {
      caja.textContent = 'No se pudo completar el análisis. Revisá la conexión e intentá nuevamente.';
    }
  };
})();
</script>
'''


def _resultado_desde_participantes(participantes):
    orden = []

    for participante in participantes or []:
        posicion = participante.get("posicion_resultado")
        try:
            posicion = int(posicion)
        except (TypeError, ValueError):
            continue

        if posicion < 1:
            continue

        orden.append({
            "posicion": posicion,
            "numero": participante.get("numero"),
            "nombre": clean(participante.get("nombre", "")),
        })

    orden.sort(key=lambda item: item["posicion"])
    if not orden:
        return None

    return {
        "orden": orden,
        "fuente": "Stud Book",
        "guardado_en": datetime.now().isoformat(timespec="seconds"),
    }


def _guardar_resultado_separado(data):
    if not isinstance(data, dict):
        return False

    fecha = clean(data.get("fecha", ""))
    hipodromo = clean(data.get("hipodromo", ""))

    try:
        numero = int(data.get("numero"))
    except (TypeError, ValueError):
        return False

    resultado = data.get("resultado_real")
    if not resultado:
        resultado = _resultado_desde_participantes(data.get("participantes", []))

    if not fecha or not hipodromo or not resultado:
        return False

    con = db()
    try:
        cursor = con.execute(
            """
            UPDATE carreras
            SET resultado_real=?
            WHERE fecha=? AND hipodromo=? AND numero=?
            """,
            (
                json.dumps(resultado, ensure_ascii=False),
                fecha,
                hipodromo,
                numero,
            ),
        )
        con.commit()
        return cursor.rowcount > 0
    finally:
        con.close()


@app.post("/api/resultado-oficial")
def guardar_resultado_oficial():
    data = request.get_json(silent=True) or {}

    if _guardar_resultado_separado(data):
        return jsonify(
            ok=True,
            mensaje="Resultado oficial guardado separado del pronóstico.",
        )

    return jsonify(
        ok=False,
        error=(
            "No se encontró la carrera guardada o faltan posiciones oficiales."
        ),
    ), 400


@app.after_request
def completar_guardado_y_pantalla(response):
    # La ruta original guarda primero el pronóstico. Después se actualiza
    # únicamente resultado_real, por lo que ambos datos permanecen separados.
    if (
        response.status_code < 400
        and request.path in {"/api/guardar", "/api/guardar-datos"}
    ):
        try:
            _guardar_resultado_separado(request.get_json(silent=True) or {})
        except Exception:
            # Un resultado oficial ausente nunca debe impedir guardar el análisis.
            pass

    # Se reemplaza el botón manual por guardado automático sin modificar
    # templates/index.html ni duplicar lógica en el archivo principal.
    if request.path == "/" and response.mimetype == "text/html":
        html = response.get_data(as_text=True)
        if 'id="lea-auto-save-v1"' not in html:
            html = html.replace("</body>", AUTO_SAVE_SCRIPT + "</body>")
            response.set_data(html)

    return response
