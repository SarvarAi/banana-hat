"""Digest format 2: entries stop hashing row ids.

Existing rows are marked format 1 and keep verifying exactly as they were written;
nothing already in the chain is rewritten. New rows are format 2.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditentry",
            name="event_ref",
            field=models.CharField(
                blank=True, max_length=40, help_text="The event's public id when the entry was written."
            ),
        ),
        migrations.AddField(
            model_name="auditentry",
            name="format",
            field=models.PositiveSmallIntegerField(default=1),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="auditentry",
            name="format",
            field=models.PositiveSmallIntegerField(
                default=2, help_text="Digest format. 1 hashed row ids; 2 hashes only text snapshots."
            ),
        ),
    ]
