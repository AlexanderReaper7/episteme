import procrastinate

from ..config import settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=settings.database_url),
    import_paths=[
        "episteme.worker.tasks",
        "episteme.worker.pipeline",
        "episteme.worker.backup",
        "episteme.worker.maintenance",
        "episteme.worker.job_history",
        "episteme.worker.topics",
        "episteme.worker.bench",
        "episteme.worker.digest",
        # A correspondent's own schedule. Its tasks live with the plugin, not in
        # worker/, because they are the plugin's (0046).
        "episteme.correspondents.matsedel.tasks",
    ],
)
