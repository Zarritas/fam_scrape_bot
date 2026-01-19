"""
Repositorio para competiciones.
"""

import json
from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database.models import Competition, Event
from src.database.repositories.base import BaseRepository


class CompetitionRepository(BaseRepository[Competition]):
    """
    Repositorio para operaciones con competiciones.
    """

    model = Competition

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_pdf_url(self, pdf_url: str) -> Competition | None:
        """Obtiene una competición por su URL de PDF."""
        result = await self.session.execute(
            select(Competition)
            .where(Competition.pdf_url == pdf_url)
            .options(selectinload(Competition.events))
        )
        return result.scalar_one_or_none()

    async def get_by_pdf_url_and_name(self, pdf_url: str, name: str) -> Competition | None:
        """Obtiene una competición por su URL de PDF y nombre."""
        result = await self.session.execute(
            select(Competition)
            .where(Competition.pdf_url == pdf_url, Competition.name == name)
            .options(selectinload(Competition.events))
        )
        return result.scalar_one_or_none()

    async def get_by_pdf_hash(self, pdf_hash: str) -> Competition | None:
        """Obtiene una competición por su hash de PDF."""
        result = await self.session.execute(
            select(Competition).where(Competition.pdf_hash == pdf_hash)
        )
        return result.scalar_one_or_none()

    async def get_upcoming(
        self,
        from_date: date | None = None,
    ) -> Sequence[Competition]:
        """
        Obtiene competiciones con alguna fecha >= from_date.

        Si from_date es None, usa la fecha actual.
        """
        if from_date is None:
            from_date = date.today()

        result = await self.session.execute(
            select(Competition).options(selectinload(Competition.events))
        )
        all_competitions = result.scalars().all()

        # Filtrar competiciones que tienen al menos una fecha >= from_date
        upcoming = [
            comp for comp in all_competitions if any(d >= from_date for d in comp.todas_las_fechas)
        ]

        # Ordenar por la primera fecha
        upcoming.sort(key=lambda c: c.todas_las_fechas[0] if c.todas_las_fechas else date.max)

        return upcoming

    async def upsert_with_hash(
        self,
        pdf_url: str | None,
        pdf_hash: str | None,
        name: str,
        dates: list[date],
        location: str,
        has_modifications: bool = False,
        competition_type: str | None = None,
        enrollment_url: str | None = None,
        events: list[dict] | None = None,
    ) -> tuple[Competition, bool]:
        """
        Inserta o actualiza una competición basándose en el hash del PDF o nombre/fechas.

        Si el PDF ya existe con el mismo hash, no hace nada.
        Si el PDF existe pero el hash cambió, actualiza.
        Si no hay PDF, busca por nombre y primera fecha para evitar duplicados.
        Si no existe, lo crea.

        Args:
            pdf_url: URL del PDF
            pdf_hash: Hash SHA-256 del contenido del PDF
            name: Nombre de la competición
            dates: Lista de fechas de la competición
            location: Lugar
            has_modifications: Si tiene marcador de modificaciones
            competition_type: Tipo de competición (PC, AL, etc.)
            enrollment_url: URL de inscripción
            events: Lista opcional de eventos a crear

        Returns:
            Tupla (Competition, is_new_or_updated)
            - is_new_or_updated es True si se creó o actualizó
        """
        # Buscar competición existente
        if pdf_url:
            # Si hay PDF, buscar por URL y nombre
            existing = await self.get_by_pdf_url_and_name(pdf_url, name)
        else:
            # Si no hay PDF, buscar por nombre para evitar duplicados
            result = await self.session.execute(
                select(Competition)
                .where(Competition.name == name)
                .options(selectinload(Competition.events))
            )
            existing = result.scalar_one_or_none()

        if existing:
            # Ya existe - verificar si el hash cambió
            # También actualizamos si ha cambiado la URL de inscritos
            # aunque el hash del PDF sea el mismo
            if existing.pdf_hash == pdf_hash and existing.enrollment_url == enrollment_url:
                # Sin cambios
                return existing, False

            # Actualizar
            # Nota: Si solo cambia el enrollment_url, también actualizamos
            # Serializar fechas para el campo JSON
            dates_json = json.dumps([d.isoformat() for d in sorted(set(dates))])

            await self.update(
                existing,
                pdf_hash=pdf_hash,
                name=name,
                competition_date=dates_json,
                location=location,
                has_modifications=has_modifications,
                competition_type=competition_type,
                enrollment_url=enrollment_url,
            )

            # Eliminar eventos antiguos y crear nuevos
            if events is not None:
                # Eliminar eventos existentes
                for event in list(existing.events):
                    await self.session.delete(event)

                # Crear nuevos eventos
                for event_data in events:
                    event = Event(competition_id=existing.id, **event_data)
                    self.session.add(event)

            await self.session.flush()
            return existing, True

        # No existe - crear nueva
        # Serializar fechas para el campo JSON
        dates_json = json.dumps([d.isoformat() for d in sorted(set(dates))])

        competition = await self.create(
            pdf_url=pdf_url,
            pdf_hash=pdf_hash,
            name=name,
            competition_date=dates_json,
            location=location,
            has_modifications=has_modifications,
            competition_type=competition_type,
            enrollment_url=enrollment_url,
        )

        # Crear eventos
        if events is not None:
            for event_data in events:
                event = Event(competition_id=competition.id, **event_data)
                self.session.add(event)
            await self.session.flush()

        return competition, True

    async def get_with_events(self, competition_id: int) -> Competition | None:
        """Obtiene una competición con sus eventos cargados."""
        result = await self.session.execute(
            select(Competition)
            .where(Competition.id == competition_id)
            .options(selectinload(Competition.events))
        )
        return result.scalar_one_or_none()

    async def count_upcoming(self, from_date: date | None = None) -> int:
        """Cuenta competiciones futuras."""
        upcoming = await self.get_upcoming(from_date)
        return len(upcoming)

    async def get_by_event_type(
        self,
        discipline: str,
        sex: str,
        from_date: date | None = None,
    ) -> Sequence[Competition]:
        """
        Obtiene competiciones que contienen una prueba específica.

        Args:
            discipline: Nombre de la disciplina (ej: "100m")
            sex: Sexo ("M", "F" o "B" para ambos)
            from_date: Fecha inicial (default: hoy)
        """
        if from_date is None:
            from_date = date.today()

        stmt = (
            select(Competition)
            .join(Competition.events)
            .where(Event.discipline == discipline)
            .options(selectinload(Competition.events))
            .distinct()
        )

        if sex != "B":
            stmt = stmt.where(Event.sex == sex)

        result = await self.session.execute(stmt)
        all_competitions = result.scalars().all()

        # Filtrar por fecha
        filtered = [
            comp for comp in all_competitions if any(d >= from_date for d in comp.todas_las_fechas)
        ]

        # Ordenar por primera fecha
        filtered.sort(key=lambda c: c.todas_las_fechas[0] if c.todas_las_fechas else date.max)

        return filtered

    async def get_by_exact_date(self, target_date: date) -> Sequence[Competition]:
        """Obtiene competiciones para una fecha específica."""
        result = await self.session.execute(
            select(Competition).options(selectinload(Competition.events))
        )
        all_competitions = result.scalars().all()

        # Filtrar competiciones que tienen la fecha exacta
        filtered = [comp for comp in all_competitions if target_date in comp.todas_las_fechas]

        return filtered

    async def delete_past_competitions(self, before_date: date) -> int:
        """
        Elimina competiciones con todas las fechas anteriores a before_date.

        Returns:
            Número de competiciones eliminadas.
        """
        import logging

        from sqlalchemy import delete

        logger = logging.getLogger(__name__)

        # Obtener todas las competiciones
        result = await self.session.execute(select(Competition))
        all_competitions = result.scalars().all()

        # Filtrar competiciones a eliminar (todas las fechas < before_date)
        to_delete = [
            comp for comp in all_competitions if all(d < before_date for d in comp.todas_las_fechas)
        ]

        if not to_delete:
            return 0

        # Log the competitions being deleted
        for comp in to_delete:
            logger.info(
                f"Eliminando competición pasada: {comp.name} (fechas: {comp.todas_las_fechas})"
            )

        # Eliminar competiciones
        competition_ids = [comp.id for comp in to_delete]
        delete_stmt = delete(Competition).where(Competition.id.in_(competition_ids))
        result = await self.session.execute(delete_stmt)

        return len(to_delete)
