"""Bot_CR data layer: Appwrite connectivity + async store.

NOTE: ``from data import store`` yields the ``Store()`` singleton (not the
``data.store`` module) because the singleton is re-exported here. If you ever
need the module itself, use ``import data.store`` / ``from data.store import``.
"""
from data.store import Store, StoreError, store
