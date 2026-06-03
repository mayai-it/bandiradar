"""SQLite store with dedupe + change detection (ARCHITECTURE.md §8).

Uses stdlib ``sqlite3`` (zero-config, agent-friendly). Tables: ``opportunities``,
``raw_docs``, ``matches``, ``runs``. ``upsert_opportunity`` dedupes by ``id`` and
detects changes via ``content_hash`` -> bump ``version``, set status
``amended``, and flag the row as re-notifiable (a tender *rettifica* should
re-notify). Also provides ``list_new(since)`` and ``save_match`` / ``get_matches``.

TODO(Prompt 5): implement the SQLite store, dedupe, and change detection.
"""
