from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import JsonResponse, HttpResponse
from django.views import generic
from django.contrib import messages
import requests
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache


API_URL = f"{settings.API_BASE_URL}/fallas"

# Sesion HTTP a nivel de modulo: se crea una vez y reusa la conexion TCP
# con el api/ en vez de abrir una nueva por cada request (menos latencia).
SESSION = requests.Session()

# Severidad, tipo de falla y estados casi no cambian: cache de 2 min.
# Maquinas se da de alta más seguido, asi que su cache es mucho mas corto
# (30 seg) para que una maquina nueva aparezca casi al instante.
# La lista de reportes cambia seguido pero no necesitas verla al segundo:
# 15 seg. El dashboard de usuarios comparte este mismo cache.
CATALOGOS_TTL = 60 * 2
MAQUINAS_TTL = 30
REPORTES_TTL = 15


def _cargar_catalogos():
    """Devuelve (severidades, tipos_falla, maquinas, estados, trabajadores, ok).
    Los toma del cache si estan todos; si falta alguno, pide todos juntos en
    UNA sola llamada al endpoint agregador del api/ y los vuelve a cachear
    cada uno con su propio TTL."""
    severidades = cache.get("fallas_severidades")
    tipos_falla = cache.get("fallas_tipos_falla")
    maquinas = cache.get("fallas_maquinas")
    estados = cache.get("fallas_estados")
    trabajadores = cache.get("fallas_trabajadores")

    if None not in (severidades, tipos_falla, maquinas, estados, trabajadores):
        return severidades, tipos_falla, maquinas, estados, trabajadores, True

    try:
        data = SESSION.get(f"{API_URL}/v1/catalogos-reporte/", timeout=5).json()
    except requests.exceptions.RequestException:
        return [], [], [], [], [], False

    severidades = data["severidades"]
    tipos_falla = data["tipos_falla"]
    maquinas = data["maquinas"]
    estados = data["estados"]
    trabajadores = data.get("trabajadores", [])

    cache.set("fallas_severidades", severidades, CATALOGOS_TTL)
    cache.set("fallas_tipos_falla", tipos_falla, CATALOGOS_TTL)
    cache.set("fallas_maquinas", maquinas, MAQUINAS_TTL)
    cache.set("fallas_estados", estados, CATALOGOS_TTL)
    cache.set("fallas_trabajadores", trabajadores, CATALOGOS_TTL)

    return severidades, tipos_falla, maquinas, estados, trabajadores, True


def _detalle_error_api(response):
    """Convierte el JSON de error de DRF ({'campo': ['msg']}) en un texto
    legible (solo los mensajes, sin etiquetas de campo) para el toast."""
    try:
        cuerpo = response.json()
    except ValueError:
        return f"El API respondió {response.status_code}."
    if isinstance(cuerpo, dict):
        partes = []
        for errores in cuerpo.values():
            if isinstance(errores, list):
                partes.extend(str(e) for e in errores)
            else:
                partes.append(str(errores))
        return " ".join(partes)
    return str(cuerpo)


class ReporteFalla(generic.View):
    template_name = "fallas/reporte_falla.html"
    context = {}
    url_create = f"{API_URL}/v2/reportes/create/"
    response = None
    payload = {}

    def _contexto_base(self, request):
        usuario = request.session.get("usuario")
        severidades, tipos_falla, maquinas, estados, trabajadores, ok = _cargar_catalogos()
        if not ok:
            messages.warning(request, "No se pudo conectar con la API para cargar los catálogos.")
        return {
            "severidades": severidades,
            "tipos_falla": tipos_falla,
            "maquinas": maquinas,
            "estados": estados,
            "trabajadores": trabajadores,
            "seccion": "fallas",
            "subseccion": "reporte",
            "usuario": usuario,
            "es_tecnico": usuario.get("rol") == "TECNI",
            "base_template": "base_tecni.html" if usuario.get("rol") == "TECNI" else "base_admin.html",
        }

    def get(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")

        self.context = self._contexto_base(request)
        return render(request, self.template_name, self.context)

    def post(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")

        self.payload = {
            "asunto": request.POST.get("asunto"),
            "descripcion": request.POST.get("descripcion"),
            "causaRaiz": request.POST.get("causaRaiz"),
            "tiempoParo": request.POST.get("tiempoParo"),
            "maquina": request.POST.get("maquina"),
            "trabajador": request.POST.get("trabajador"),
            "tipo_severidad": request.POST.get("tipo_severidad"),
        }

        tipo_falla_ids = []
        valor_base = request.POST.get("tipo_falla")
        if valor_base:
            tipo_falla_ids.append(int(valor_base))

        idx = 1
        while True:
            val = request.POST.get(f"tipo_falla_{idx}")
            if val is None:
                break
            if val:
                tipo_falla_ids.append(int(val))
            idx += 1

        self.payload["tipo_falla_ids"] = tipo_falla_ids

        archivo = request.FILES.get("imagen")
        files = {"imagen": archivo} if archivo else None
        try:
            self.response = SESSION.post(url=self.url_create, data=self.payload, files=files, timeout=10)
        except requests.exceptions.RequestException:
            messages.warning(request, "No se pudo conectar con la API para registrar el reporte")
            return self._render_error(request)
        if self.response.status_code == 201:
            # invalidar el cache de la lista: el reporte recien creado debe
            # aparecer de inmediato en "Ver reportes" y en el dashboard.
            cache.delete("fallas_reportes_list")
            creado = self.response.json()
            messages.success(request, "El reporte de falla ha sido registrado")
            # Solo el admin recibe la sugerencia de levantar una orden de
            # mantenimiento; el tecnico se va directo a la lista.
            if usuario.get("rol") == "TECNI":
                return redirect("fallas:lista")
            return redirect(
                "{}?levantar_orden=1&reporte={}&asunto={}&maquina={}".format(
                    reverse("fallas:lista"),
                    creado.get("numeroRegistro"),
                    quote(creado.get("asunto") or ""),
                    quote(self.payload.get("maquina") or ""),
                )
            )
        else:
            messages.warning(request, f"No se pudo registrar el reporte: {_detalle_error_api(self.response)}")
            return self._render_error(request)

    def _render_error(self, request):
        context = self._contexto_base(request)
        context["datos"] = self.payload
        return render(request, self.template_name, context)

#---------------------------------------------------------------------------
#------------TIPO FALLA ----------------------------------------------------
#---------------------------------------------------------------------------
class ListTipoFalla(generic.View):
    template_name = "fallas/list_tipo_falla.html"
    context = {}
    url_base = f"{API_URL}/v1/tipo_falla/list/"
    response = None

class ListaReportes(generic.View):
    template_name = "fallas/lista_reportes.html"
    context = {}
    url_base = f"{API_URL}/v1/reportes/list/"
    response = None

    def get(self, request):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")

        # mismo cache key que usa el dashboard de usuarios: si alguien entro
        # a cualquiera de las dos pantallas hace menos de 15 seg, se reusa.
        reportes = cache.get("fallas_reportes_list")
        if reportes is None:
            try:
                reportes = SESSION.get(url=self.url_base, timeout=5).json()
                cache.set("fallas_reportes_list", reportes, REPORTES_TTL)
            except (requests.exceptions.RequestException, ValueError):
                reportes = []
                messages.warning(request, "No se pudo conectar con la API para cargar los reportes.")

        severidades, tipos_falla, maquinas, estados, trabajadores, _ = _cargar_catalogos()

        self.context = {
            "reportes": reportes,
            "severidades": severidades,
            "maquinas": maquinas,
            "seccion": "fallas",
            "subseccion": "lista",
            "usuario": usuario,
            "base_template": "base_tecni.html" if usuario.get("rol") == "TECNI" else "base_admin.html",
        }
        return render(request, self.template_name, self.context)
    

class DetailReporte(generic.View):
    template_name = "fallas/fallas-modal/ver-detalle.html"
    template_pagina = "fallas/detalle_reporte_pagina.html"
    context = {}

    def get(self, request, pk):
        es_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        if not es_ajax:
            usuario = request.session.get("usuario")
            if not usuario:
                messages.warning(request, "Inicia sesión para continuar.")
                return redirect("usuarios:index")

        cache_key = f"fallas_reporte_{pk}"
        reporte = cache.get(cache_key)

        if reporte is None:
            try:
                resp = SESSION.get(f"{API_URL}/v1/reportes/{pk}/", timeout=5)
                if resp.status_code != 200:
                    reporte = None
                else:
                    reporte = resp.json()
                    cache.set(cache_key, reporte, 30)
            except (requests.exceptions.RequestException, ValueError):
                reporte = None

        if es_ajax:
            self.context = {"reporte": reporte}
            return render(request, self.template_name, self.context)

        # visita directa por URL: pagina completa con layout y CSS
        usuario = request.session.get("usuario")
        self.context = {
            "reporte": reporte,
            "modo_pagina": True,
            "seccion": "fallas",
            "subseccion": "lista",
            "usuario": usuario,
            "base_template": "base_tecni.html" if usuario.get("rol") == "TECNI" else "base_admin.html",
        }
        return render(request, self.template_pagina, self.context)

    def _enriquecer_fallas(self, reporte):
        """Garantiza que el detalle liste TODAS las fallas adjuntadas al
        reporte. Si el API ya trae `fallas_asociadas` con nombre se respeta;
        si viene vacía o falta, se reconstruye desde `tipo_falla_ids`
        cotejando contra el catálogo (misma lógica que ActualizarReporte)."""
        if not isinstance(reporte, dict):
            return
        if reporte.get("fallas_asociadas"):
            return

        fallas_ids = reporte.get("tipo_falla_ids") or []
        if not fallas_ids:
            return

        _, tipos_falla, _, _, _, _ = _cargar_catalogos()
        fallas_asociadas = []
        for fid in fallas_ids:
            coincidencia = [t for t in tipos_falla if t.get("numeroRegistro") == fid]
            if coincidencia:
                fallas_asociadas.append({
                    "id": fid,
                    "nombre": coincidencia[0].get("nombre"),
                })
        if fallas_asociadas:
            reporte["fallas_asociadas"] = fallas_asociadas


class ActualizarReporte(generic.View):
    template_name = "fallas/actualizar_reporte.html"

    def _cargar_reporte(self, request, pk):
        cache_key = f"fallas_reporte_{pk}"
        reporte = cache.get(cache_key)
        if reporte is not None:
            return reporte
        try:
            resp = SESSION.get(f"{API_URL}/v1/reportes/{pk}/", timeout=5)
            if resp.status_code != 200:
                messages.warning(request, "No se pudo cargar el reporte.")
                return None
            reporte = resp.json()
            cache.set(cache_key, reporte, 30)
            return reporte
        except (requests.exceptions.RequestException, ValueError):
            messages.warning(request, "No se pudo conectar con la API.")
            return None

    def _contexto(self, request, reporte):
        severidades, tipos_falla, maquinas, estados, trabajadores, _ = _cargar_catalogos()
        return {
            "reporte": reporte,
            "severidades": severidades,
            "tipos_falla": tipos_falla,
            "maquinas": maquinas,
            "estados": estados,
            "trabajadores": trabajadores,
            "usuario": request.session.get("usuario"),
            "base_template": "base_tecni.html" if request.session.get("usuario", {}).get("rol") == "TECNI" else "base_admin.html",
        }

    def get(self, request, pk):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")

        reporte = self._cargar_reporte(request, pk)
        if reporte is None:
            return redirect("fallas:lista")

        if not self._puede_editar(request, reporte):
            messages.warning(request, "No tienes permisos para actualizar este reporte.")
            return redirect("fallas:lista")

        return render(request, self.template_name, self._contexto(request, reporte))

    def post(self, request, pk):
        usuario = request.session.get("usuario")
        if not usuario:
            messages.warning(request, "Inicia sesión para continuar.")
            return redirect("usuarios:index")

        reporte = self._cargar_reporte(request, pk)
        if reporte is None:
            return redirect("fallas:lista")

        if not self._puede_editar(request, reporte):
            messages.warning(request, "No tienes permisos para actualizar este reporte.")
            return redirect("fallas:lista")

        payload = {
            "asunto": request.POST.get("asunto"),
            "descripcion": request.POST.get("descripcion"),
            "causaRaiz": request.POST.get("causaRaiz"),
            "tiempoParo": request.POST.get("tiempoParo"),
            "maquina": request.POST.get("maquina"),
            "trabajador": request.POST.get("trabajador"),
            "tipo_severidad": request.POST.get("tipo_severidad"),
            "estado_reporte": request.POST.get("estado_reporte"),
        }

        tipo_falla_ids = []
        valor_base = request.POST.get("tipo_falla")
        if valor_base:
            tipo_falla_ids.append(int(valor_base))
        idx = 1
        while True:
            val = request.POST.get(f"tipo_falla_{idx}")
            if val is None:
                break
            if val:
                tipo_falla_ids.append(int(val))
            idx += 1
        payload["tipo_falla_ids"] = tipo_falla_ids

        archivo = request.FILES.get("imagen")
        files = {"imagen": archivo} if archivo else None

        try:
            api_url = f"{API_URL}/v2/reportes/update/{pk}/"
            response = SESSION.patch(url=api_url, data=payload, files=files, timeout=10)
        except requests.exceptions.RequestException:
            messages.warning(request, "No se pudo conectar con la API para actualizar el reporte.")
            return self._render_error(request, pk, payload)
        if response.status_code == 200:
            cache.delete(f"fallas_reporte_{pk}")
            cache.delete("fallas_reportes_list")
            messages.success(request, "El reporte ha sido actualizado correctamente.")
            return redirect("fallas:lista")
        else:
            messages.warning(request, "Error al actualizar el reporte.")
            return self._render_error(request, pk, payload)

    def _puede_editar(self, request, reporte):
        """El tecnico solo puede actualizar sus propios reportes; el resto
        de roles (admin) puede actualizar cualquiera."""
        usuario = request.session.get("usuario")
        if not usuario:
            return False
        if usuario.get("rol") != "TECNI":
            return True
        return reporte.get("trabajador") == usuario.get("numeroNomina")

    def _render_error(self, request, pk, payload):
        reporte = self._cargar_reporte(request, pk)
        if reporte is None:
            return redirect("fallas:lista")

        reporte = dict(reporte)
        for campo in ("asunto", "descripcion", "causaRaiz", "tiempoParo",
                      "maquina", "trabajador", "tipo_severidad",
                      "estado_reporte"):
            if campo in payload:
                reporte[campo] = payload[campo]

        fallas_ids = payload.get("tipo_falla_ids", [])
        reporte["fallas_asociadas"] = []
        severidades, tipos_falla, maquinas, estados, trabajadores, _ = _cargar_catalogos()
        for fid in fallas_ids:
            coincidencia = [t for t in tipos_falla if t.get("numeroRegistro") == fid]
            if coincidencia:
                reporte["fallas_asociadas"].append({"id": fid, "nombre": coincidencia[0].get("nombre")})

        return render(request, self.template_name, self._contexto(request, reporte))


class InvalidarCacheReportes(generic.View):

    def post(self, request):
        cache.delete("fallas_reportes_list")
        return JsonResponse({"ok": True})


# ------------ EXPORTACIONES (proxy a la API) -------------------------
class _ExportarBase(generic.View):
    formato = None  # "csv", "xlsx", "pdf"

    def get(self, request, pk):
        try:
            resp = SESSION.get(f"{API_URL}/v1/reportes/{pk}/export/{self.formato}/", timeout=15)
        except requests.exceptions.RequestException:
            return HttpResponse("Error de conexion con la API", status=502)

        if resp.status_code != 200:
            return HttpResponse("Error al generar el archivo", status=resp.status_code)

        response = HttpResponse(resp.content, content_type=resp.headers.get("Content-Type", "application/octet-stream"))
        response["Content-Disposition"] = resp.headers.get("Content-Disposition", f'attachment; filename="reporte_falla_{pk}.{self.formato}"')
        return response


class ExportarReporteCSV(_ExportarBase):
    formato = "csv"


class ExportarReporteXLSX(_ExportarBase):
    formato = "xlsx"


class ExportarReportePDF(_ExportarBase):
    formato = "pdf"