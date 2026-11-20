"""Container targets pin their image (B-45: NFR-REP-1).

A run that acts on a container should act on the same software every time it is
repeated. ``environment_targets.image_digest`` records the image a container target
runs, as Docker's content-addressed image ID (``sha256:<64 hex>``, what
``docker inspect --format '{{.Image}}' <container>`` prints). A run pins the digests of
its environment's container targets when it is queued, the worker signs the pinned
digest into each container fault, and the chaos agent refuses a container running any
other image.

* Only a container target has an image; the API requires one for every new container
  target. Targets registered before this migration keep a NULL digest: a run on them
  still works, and its evidence report says the image was not pinned.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-20
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE rai.environment_targets
    ADD COLUMN image_digest text
        CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT environment_targets_image_on_containers
        CHECK (image_digest IS NULL OR kind = 'container');
"""

DOWNGRADE = """
ALTER TABLE rai.environment_targets
    DROP CONSTRAINT environment_targets_image_on_containers,
    DROP COLUMN image_digest;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
