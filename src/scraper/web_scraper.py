"""
Web scraper para el calendario de la Federación de Atletismo de Madrid.

Extrae la lista de competiciones del calendario web, incluyendo:
- Nombre de la competición
- Fecha
- URL del PDF
- Indicador de modificaciones (fondo amarillo)
"""

import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from src.config import settings
from src.scraper.models import RawCompetition
from src.utils.logging import get_logger

logger = get_logger(__name__)


def clean_pdf_url(pdf_url: str | None) -> str | None:
    """
    Limpia una URL de PDF eliminando todo el texto que aparezca después de '.pdf'.

    Esto es útil cuando las URLs tienen parámetros adicionales que no son parte
    del archivo PDF real.

    Args:
        pdf_url: URL del PDF que puede contener texto adicional después de .pdf

    Returns:
        URL limpia que termina exactamente en .pdf, o None si la entrada es None
    """
    if not pdf_url:
        return pdf_url

    # Buscar la posición de '.pdf' (case insensitive)
    pdf_pos = pdf_url.lower().find(".pdf")
    if pdf_pos == -1:
        return pdf_url

    # Cortar justo después de '.pdf' (4 caracteres: .pdf)
    clean_url = pdf_url[: pdf_pos + 4]

    logger.debug(f"Cleaned PDF URL: {pdf_url} -> {clean_url}")
    return clean_url


# Meses en español para conversión de fechas
MONTHS_ES: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# Diccionario inverso para obtener nombre del mes por número
MONTHS_BY_NUMBER = {v: k for k, v in MONTHS_ES.items()}


class WebScraperError(Exception):
    """Error durante el scraping web."""

    pass


class WebScraper:
    """
    Scraper para el calendario de competiciones de la FAM.

    Extrae competiciones del calendario web filtrando por mes y año.

    Estructura HTML del calendario FAM:
    - Tabla con clase 'calendario' dentro de div#calendario
    - Columnas: Fecha | Límite inscripción | Competición | Lugar | regl. | insc. | | Tipo
    - El enlace al PDF está en la columna 'regl.' (no en el nombre de la competición)
    """

    def __init__(
        self,
        base_url: str | None = None,
        calendar_path: str | None = None,
        timeout: int = 30,
    ):
        self.base_url = base_url or settings.fam_base_url
        self.calendar_path = calendar_path or settings.fam_calendar_path
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            }
        )

    def _build_calendar_url(self, month: int, year: int) -> str:
        """
        Construye la URL del calendario con filtros de mes y año.

        La web FAM usa parámetros en la URL para filtrar:
        - temporada: año (ej: 2026)
        - mes: número de mes (1-12)
        """
        base = f"{self.base_url}{self.calendar_path}"
        # Añadir parámetros de temporada y mes
        separator = "&" if "?" in self.calendar_path else "?"
        return f"{base}{separator}temporada={year}&mes={month}"

    def get_competitions(
        self,
        month: int,
        year: int,
    ) -> list[RawCompetition]:
        """
        Obtiene la lista de competiciones para un mes y año específicos.

        Args:
            month: Mes (1-12)
            year: Año (ej: 2026)

        Returns:
            Lista de RawCompetition con los datos extraídos

        Raises:
            WebScraperError: Si hay error en la petición o parsing
        """
        url = self._build_calendar_url(month, year)
        logger.info(f"Scraping calendario: {url}")

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Error en petición HTTP: {e}")
            raise WebScraperError(f"Error obteniendo calendario: {e}") from e

        try:
            return self.parse_calendar_html(response.text, year)
        except Exception as e:
            logger.error(f"Error parseando HTML: {e}")
            raise WebScraperError(f"Error parseando calendario: {e}") from e

    def parse_calendar_html(self, html: str, default_year: int = 2026) -> list[RawCompetition]:
        """
        Parsea el HTML del calendario FAM y extrae las competiciones.

        Estructura HTML real:
        - Tabla con clase "table table-striped table-hover"
        - Columnas: Fecha | Competición | Lugar | Documentos | Inscripciones
        - Los PDFs están en la columna "Documentos" como enlaces
        """
        soup = BeautifulSoup(html, "lxml")
        competitions: list[RawCompetition] = []

        # Buscar tabla del calendario (la estructura real del sitio FAM)
        calendar_table = soup.find("table", class_="calendario")

        if not calendar_table:
            logger.warning("No se encontró la tabla del calendario")
            return []

        # Obtener todas las filas
        all_rows = calendar_table.find_all("tr")

        # Detectar si la primera fila es un header
        rows = all_rows
        if all_rows:
            first_row = all_rows[0]
            # Check both td and th elements for header detection
            cells = first_row.find_all("td") or first_row.find_all("th")
            if cells:
                first_cell_text = cells[0].get_text(strip=True).lower()
                # Si la primera celda contiene palabras de header, saltar la primera fila
                header_keywords = ["fecha", "día", "competición", "prueba", "evento"]
                if any(keyword in first_cell_text for keyword in header_keywords):
                    rows = all_rows[1:]

        for row in rows:
            competition = self._parse_real_competition_row(row, default_year)
            if competition:
                competitions.append(competition)

        logger.info(f"Encontradas {len(competitions)} competiciones")
        return competitions

    def _parse_real_competition_row(self, row: Tag, year: int) -> RawCompetition | None:
        """
        Parsea una fila de la tabla real del calendario FAM.

        Estructura real: Fecha | Límite | Competición | Lugar | Documentos | Inscritos | Resultados | Tipo
        Maneja fechas múltiples como "17y18.01 (S-D)"
        """
        cells = row.find_all("td")
        if len(cells) < 6:  # Need at least Fecha, Límite, Competición, Lugar, Documentos, Inscritos
            return None

        # Columna 0: Fecha (ej: "03.01 (S)" o "17y18.01 (S-D)")
        date_cell = cells[0]
        date_str = date_cell.get_text(strip=True).split(" ")[0]  # Quitar día de la semana

        # Parsear fechas
        dates = self._extract_dates(date_str, year)

        # Columna 2: Competición (nombre) - skip columna 1 (Límite)
        name_cell = cells[2]
        name = name_cell.get_text(strip=True)

        # Columna 3: Lugar
        location_cell = cells[3]
        location = location_cell.get_text(strip=True) or None

        # Columna 4: Documentos (contiene el enlace al PDF)
        docs_cell = cells[4]
        pdf_url = None
        pdf_link = docs_cell.find("a")
        if pdf_link:
            pdf_href = pdf_link.get("href")
            if pdf_href:
                # Limpiar la URL eliminando todo después de .pdf
                pdf_href = clean_pdf_url(pdf_href)
                pdf_url = urljoin(self.base_url, pdf_href)

        # Columna 5: Inscritos (contiene el enlace de inscripción)
        enroll_cell = cells[5]
        enrollment_url = None
        enroll_link = enroll_cell.find("a")
        if enroll_link:
            enroll_href = enroll_link.get("href")
            if enroll_href:
                enrollment_url = urljoin(self.base_url, enroll_href)

        # Detectar modificaciones (por ahora no hay indicador visual claro)
        has_modifications = False

        # Tipo de competición (extraer del nombre o dejar como None por ahora)
        competition_type = None

        if not name or not pdf_url:
            return None

        # Crear RawCompetition con lista de fechas
        competition = RawCompetition(
            name=name,
            dates=dates,
            pdf_url=pdf_url,
            enrollment_url=enrollment_url,
            has_modifications=has_modifications,
            location=location,
            competition_type=competition_type,
        )

        return competition

    def _parse_competition_row(
        self,
        row: Tag,
        year: int,
    ) -> RawCompetition | None:
        """
        Parsea una fila de la tabla para extraer datos de competición.

        Usa la celda del reglamento (PDF) como ancla para encontrar:
        - Nombre: ancla - 2
        - Lugar: ancla - 1
        - Inscritos: ancla + 1
        """
        cells = row.find_all("td")
        if not cells:
            return None

        # 1. Encontrar celda ancla (Reglamento/PDF)
        regl_index = -1

        # Búsqueda por contenido (más robusta)
        for i, cell in enumerate(cells):
            # Buscar enlace con "regl" en texto o título
            if (
                cell.find("a", string=lambda x: x and "regl" in x.lower())
                or cell.find("a", title=lambda x: x and "Reglamento" in x)
                or cell.find("span", class_="reglamento_circular")
            ):
                regl_index = i
                break

        # Fallback a posición fija si no se detecta (el calendario suele ser fijo)
        if regl_index == -1:
            if len(cells) > 4 and cells[4].find("a"):
                regl_index = 4
            else:
                return None

        # 2. Extraer URL del PDF (Ancla)
        regl_cell = cells[regl_index]
        regl_link = regl_cell.find("a")
        if not regl_link:
            # Intentar dentro de span
            span = regl_cell.find("span", class_="reglamento_circular")
            if span:
                regl_link = span.find("a")

        if not regl_link:
            return None

        pdf_url = regl_link.get("href", "")
        if not pdf_url:
            return None

        # Limpiar la URL eliminando todo después de .pdf
        pdf_url = clean_pdf_url(pdf_url)
        pdf_url = urljoin(self.base_url, pdf_url)

        # 3. Extraer Nombre (Ancla - 2)
        name_index = regl_index - 2
        name = "Competición sin nombre"
        if name_index >= 0 and name_index < len(cells):
            name_cell = cells[name_index]
            name_link = name_cell.find("a")
            name = name_link.get_text(strip=True) if name_link else name_cell.get_text(strip=True)

        # 4. Extraer Lugar (Ancla - 1)
        loc_index = regl_index - 1
        location = None
        if loc_index >= 0 and loc_index < len(cells):
            location = cells[loc_index].get_text(strip=True) or None

        # 5. Extraer Inscritos (Ancla + 1)
        enroll_index = regl_index + 1
        enrollment_url = None
        if enroll_index < len(cells):
            enroll_cell = cells[enroll_index]
            enroll_link = enroll_cell.find("a")
            if enroll_link:
                e_url = enroll_link.get("href", "")
                if e_url:
                    enrollment_url = urljoin(self.base_url, e_url)

        # 6. Extraer Fecha (Siempre columna 0?)
        # Asumimos columna 0 para fecha
        date_str = ""
        if len(cells) > 0:
            date_str = cells[0].get_text(strip=True)

        dates = self._extract_dates(date_str, year)

        # 7. Tipo de competición (Última columna o Ancla + 3?)
        # La estructura típica es: ... | Regl | Insc | ? | Tipo
        # Regl=4, Insc=5, ?=6, Tipo=7. Diff = +3
        type_index = regl_index + 3
        comp_type = None
        if type_index < len(cells):
            comp_type = cells[type_index].get_text(strip=True)

        # Detectar modificaciones
        has_modifications = self._has_highlight_background(row)

        return RawCompetition(
            name=name,
            dates=dates,
            pdf_url=pdf_url,
            enrollment_url=enrollment_url,
            has_modifications=has_modifications,
            location=location,
            competition_type=comp_type,
        )

    def _extract_enrollment_url(self, cells: list[Tag]) -> str | None:
        """Extrae la URL de inscripción de la celda correspondiente."""
        if len(cells) <= 5:
            return None

        cell = cells[5]
        link = cell.find("a")
        if not link:
            return None

        url = link.get("href", "")
        if not url:
            return None

        return urljoin(self.base_url, url)

    def _extract_dates(self, date_str: str, year: int) -> list[date]:
        """
        Extrae una lista de fechas desde un string de fecha del calendario.

        Formatos soportados:
        - "31.01" -> [date(year, 1, 31)]
        - "10.02" -> [date(year, 2, 10)]
        - "05.01" -> [date(year, 1, 5)]
        - "17y18.01" -> [date(year, 1, 17), date(year, 1, 18)]
        - "17,18.01" -> [date(year, 1, 17), date(year, 1, 18)]
        - "24-25.01" -> [date(year, 1, 24), date(year, 1, 25)]
        - "24-26.01" -> [date(year, 1, 24), date(year, 1, 25), date(year, 1, 26)]

        Args:
            date_str: String de fecha crudo (ej: "17y18.01")
            year: Año de la competición

        Returns:
            Lista de objetos date ordenados
        """
        dates = []
        if not date_str:
            return dates

        # Limpiar paréntesis y espacios extra (ej: "17y18.01 (S-D)")
        cleaned_str = re.sub(r"\s+\([^\)]+\)", "", date_str).strip()
        cleaned_str = cleaned_str.replace(" ", "")  # Quitar espacios internos

        try:
            # 1. Detectar separador de mes (usualmente un punto al final: .01)
            # Buscamos el último punto o barra que separa el mes
            match = re.search(r"[\./-](\d{2})$", cleaned_str)
            if not match:
                return dates

            month_str = match.group(1)
            month = int(month_str)

            # La parte de los días es todo lo anterior al separador+mes
            days_part = cleaned_str[: match.start()]

            # Normalizar separadores de días (y, -) a comas para procesamiento uniforme
            # "17y18" -> "17,18"; "17-19" -> "17-19" (rango)

            # Caso especial: Rangos con guión "24-26"
            if "-" in days_part:
                # Asumimos rango simple día-día
                start_day, end_day = map(int, days_part.split("-"))
                for day in range(start_day, end_day + 1):
                    dates.append(date(year, month, day))
                return dates

        except Exception:
            # Si falla el anterior, probar otros formatos
            pass

        try:
            # Caso rango cruzando meses/formatos complejos: "27.02-01.03"
            range_match = re.match(r"(\d{1,2})\.(\d{2})-(\d{1,2})\.(\d{2})", cleaned_str)
            if range_match:
                d1, m1, d2, m2 = map(int, range_match.groups())

                # Fecha inicio
                date1 = date(year, m1, d1)
                # Fecha fin (cuidado con cambio de año, aunque raro en una temporada)
                date2 = date(year, m2, d2)

                if date1 <= date2:
                    current = date1
                    while current <= date2:
                        dates.append(current)
                        current += timedelta(days=1)
                return dates
        except Exception as e:
            logger.warning(f"Error parseando rango complejo '{date_str}': {e}")

        try:
            # Volver a intentar el bloque original si no era rango complejo y falló el primer bloque
            # Re-implementar logica original para listas '17y18'
            # 1. Detectar separador de mes ... (reparando el flujo)
            pass  # El bloque original estaba dentro de un try... vamos a reestructurar un poco

            # Caso lista de días: "17y18", "17,18"
            days_part = days_part.replace("y", ",")
            day_strs = days_part.split(",")

            for day_s in day_strs:
                if day_s.isdigit():
                    dates.append(date(year, month, int(day_s)))

        except Exception as e:
            logger.warning(f"Error parseando fecha '{date_str}': {e}")

        return sorted(set(dates))

    def _has_highlight_background(self, element: Tag) -> bool:
        """
        Detecta si un elemento tiene fondo destacado (amarillo/verde).

        Las competiciones externas/especiales tienen:
        style='background:#EBFFAA;font-style:italic;'
        """
        if not element:
            return False

        style = element.get("style", "")
        if style:
            style_lower = style.lower()
            # Buscar colores de fondo que indican competición especial
            highlight_colors = [
                "#ebffaa",  # Verde claro usado en la FAM
                "yellow",
                "#ffff",
                "#ff0",
                "#ffc",
                "#ffd",
                "#ffe",
            ]
            for color in highlight_colors:
                if color in style_lower:
                    return True

        return False

    def _has_yellow_background(self, element: Tag) -> bool:
        """Alias para compatibilidad."""
        return self._has_highlight_background(element)

    def download_pdf(self, url: str) -> bytes:
        """
        Descarga el contenido binario de un PDF.

        Args:
            url: URL del PDF a descargar

        Returns:
            Contenido binario del PDF

        Raises:
            WebScraperError: Si hay error en la descarga
        """
        logger.info(f"Descargando PDF: {url}")

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            # Verificar que es un PDF
            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
                logger.warning(f"Contenido no parece ser PDF: {content_type}")

            return response.content

        except requests.RequestException as e:
            logger.error(f"Error descargando PDF: {e}")
            raise WebScraperError(f"Error descargando PDF: {e}") from e

    def get_competitions_for_months(
        self,
        months: list[tuple[int, int]],
    ) -> list[RawCompetition]:
        """
        Obtiene competiciones para múltiples meses desde el calendario completo.

        Args:
            months: Lista de tuplas (mes, año) - usado para filtrado posterior

        Returns:
            Lista combinada de competiciones
        """
        try:
            # Obtener el calendario completo (sin parámetros de mes/año específicos)
            url = f"{self.base_url}{self.calendar_path}"
            logger.info(f"Obteniendo calendario completo: {url}")

            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            # Parsear todas las competiciones
            all_competitions = self.parse_calendar_html(response.text, 2026)  # Valores por defecto

            # Filtrar por los meses solicitados
            filtered_competitions = []
            for comp in all_competitions:
                # Verificar si alguna de las fechas de la competición cae en los meses solicitados
                include_comp = False
                for d in comp.dates:
                    if (d.month, d.year) in months:
                        include_comp = True
                        break

                if include_comp:
                    filtered_competitions.append(comp)

            logger.info(
                f"Filtradas {len(filtered_competitions)} competiciones para los meses solicitados"
            )
            return filtered_competitions

        except Exception as e:
            logger.error(f"Error obteniendo calendario completo: {e}")
            raise WebScraperError(f"Error obteniendo calendario: {e}") from e


def get_current_and_next_months() -> list[tuple[int, int]]:
    """
    Obtiene el mes actual y el siguiente.

    Maneja el cambio de año (diciembre → enero).

    Returns:
        Lista de tuplas (mes, año) para mes actual y siguiente
    """
    today = date.today()
    current_month = today.month
    current_year = today.year

    # Mes siguiente
    if current_month == 12:
        next_month = 1
        next_year = current_year + 1
    else:
        next_month = current_month + 1
        next_year = current_year

    return [
        (current_month, current_year),
        (next_month, next_year),
    ]


def parse_date_string(date_str: str, year: int) -> date | None:
    """
    Convierte una cadena de fecha al objeto date.

    Args:
        date_str: Fecha como string (ej: "11 de enero", "11/01")
        year: Año para completar la fecha

    Returns:
        Objeto date o None si no se puede parsear
    """
    if not date_str:
        return None

    # Patrón: "11 de enero" o "11 enero"
    match = re.search(r"(\d{1,2})\s*(?:de\s+)?(\w+)", date_str, re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month_str = match.group(2).lower()
        month = MONTHS_ES.get(month_str)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    # Patrón: "11/01"
    match2 = re.search(r"(\d{1,2})[/-](\d{1,2})", date_str)
    if match2:
        day = int(match2.group(1))
        month = int(match2.group(2))
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None
