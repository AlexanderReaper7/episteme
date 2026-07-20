import procrastinate

from ..config import settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=settings.database_url),
    import_paths=[
        "episteme.worker.tasks",
        "episteme.worker.pipeline",
        "episteme.worker.backup",
    ],
)
