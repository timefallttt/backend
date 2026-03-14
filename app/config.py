import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
RUNTIME_DIR = Path(os.getenv('APP_RUNTIME_DIR', BASE_DIR / 'runtime'))
INDEXING_DIR = RUNTIME_DIR / 'indexing'
INDEXING_JOBS_FILE = INDEXING_DIR / 'jobs.json'
REPO_STORAGE_DIR = INDEXING_DIR / 'repos'
ARTIFACT_STORAGE_DIR = INDEXING_DIR / 'artifacts'
REVIEW_DIR = RUNTIME_DIR / 'review'
REVIEW_TASKS_FILE = REVIEW_DIR / 'tasks.json'

ARKANALYZER_CMD = os.getenv('ARKANALYZER_CMD', '').strip()
ARKANALYZER_ROOT = Path(os.getenv('ARKANALYZER_ROOT', PROJECT_ROOT / 'arkanalyzer'))
ARKANALYZER_ENTRY = Path(os.getenv('ARKANALYZER_ENTRY', 'lib/save/serializeArkIR.js'))
ARKANALYZER_TIMEOUT_SEC = int(os.getenv('ARKANALYZER_TIMEOUT_SEC', '1800'))

NEO4J_URI = os.getenv('NEO4J_URI', 'bolt://localhost:7687').strip()
NEO4J_USERNAME = os.getenv('NEO4J_USERNAME', 'neo4j').strip()
NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD', '12345678').strip()
NEO4J_DATABASE = os.getenv('NEO4J_DATABASE', 'neo4j').strip()
