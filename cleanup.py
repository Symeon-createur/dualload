import os
import time
import glob
from datetime import datetime, timedelta
import logging

# Configuration
DOWNLOAD_FOLDER = os.path.abspath("downloads")
TEMP_FOLDER = os.path.abspath("temp_downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Journalisation
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cleanup.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def is_file_locked(filepath):
    """Vérifie si un fichier est verrouillé"""
    try:
        with open(filepath, 'a') as f:
            pass
        return False
    except (IOError, PermissionError):
        return True

def safe_delete(filepath):
    """Suppression sécurisée"""
    try:
        if os.path.exists(filepath):
            if is_file_locked(filepath):
                return False
            os.remove(filepath)
            return True
    except Exception as e:
        logger.warning(f"Échec suppression {filepath}: {e}")
    return False

def cleanup_cycle():
    """Exécute un cycle de nettoyage complet"""
    now = datetime.now()
    
    # Nettoyage des fichiers temporaires (>30 min)
    for filepath in glob.glob(os.path.join(TEMP_FOLDER, '*')):
        try:
            file_age = now - datetime.fromtimestamp(os.path.getmtime(filepath))
            if file_age > timedelta(minutes=30):
                if safe_delete(filepath):
                    logger.info(f"Supprimé: {filepath}")
        except Exception as e:
            logger.error(f"Erreur vérification {filepath}: {e}")
    
    # Nettoyage des downloads (>24h)
    for filepath in glob.glob(os.path.join(DOWNLOAD_FOLDER, '*')):
        try:
            file_age = now - datetime.fromtimestamp(os.path.getmtime(filepath))
            if file_age > timedelta(hours=24):
                if safe_delete(filepath):
                    logger.info(f"Supprimé: {filepath}")
        except Exception as e:
            logger.error(f"Erreur vérification {filepath}: {e}")

def main():
    print("Service de nettoyage démarré")
    try:
        while True:
            cleanup_cycle()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nNettoyage arrêté proprement")
    except Exception as e:
        print(f"\nERREUR NETTOYAGE: {str(e)}")

if __name__ == "__main__":
    print("Service de nettoyage actif (Ctrl+C pour arrêter)")
    try:
        while True:
            cleanup_cycle()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n[ARRÊT] Nettoyage terminé")
    except Exception as e:
        print(f"\n[ERREUR] {str(e)}")