import asyncio
import logging
import sys

from src.scraper.pdf_parser import PDFParser
from src.scraper.web_scraper import WebScraper

# Configurar logging
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logging.getLogger("src").setLevel(logging.DEBUG)
logging.getLogger("pdfminer").setLevel(logging.WARNING)


async def debug_pdf():
    # URL del PDF que dio problemas
    url = "https://www.atletismomadrid.com/images/stories/ficheros/eventos/reglamentos/gallur_2026_01_31.pdf"

    scraper = WebScraper()
    print(f"Descargando PDF: {url}")
    try:
        pdf_content = scraper.download_pdf(url)
    except Exception as e:
        print(f"Error descargando: {e}")
        return

    parser = PDFParser()

    # Optional raw debug
    # print("\n--- DEBUG: RAW PDF CONTENT ---")
    # ... code commented out to reduce noise ...
    # print("--- END DEBUG ---\n")

    print("Parseando PDF con parser...")
    try:
        competition = parser.parse(pdf_content, name="Debug Competition")
        print(f"Eventos encontrados: {len(competition.events)}")
        for event in competition.events:
            print(f" - {event.discipline} ({event.sex}) [{event.event_type}]")

        # Verificar si está "Pértiga"
        has_pertiga = any(
            "pertiga" in e.discipline.lower() or "pértiga" in e.discipline.lower()
            for e in competition.events
        )
        print(f"\n¿Contiene Pértiga?: {has_pertiga}")

    except Exception as e:
        print(f"Error parseando: {e}")


if __name__ == "__main__":
    asyncio.run(debug_pdf())
