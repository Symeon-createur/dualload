from flask import Flask, render_template, request, send_file, redirect, url_for, flash, get_flashed_messages
import os
import yt_dlp
from urllib.parse import urlparse
import logging
import zipfile
import shutil
import re
import time
import threading
import ctypes
import stat
from datetime import datetime, timedelta
import atexit
from threading import Timer
import sys

# Initialisation de l'application Flask
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuration des dossiers
DOWNLOAD_FOLDER = os.path.abspath("downloads")
TEMP_FOLDER = os.path.abspath("temp_downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Correction pour l'encodage Windows
if os.name == 'nt':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

# Variables globales pour la gestion des téléchargements
ACTIVE_DOWNLOADS = set()
DOWNLOAD_LOCK = threading.Lock()

def cleanup_on_exit():
    """Nettoyage des fichiers temporaires à l'arrêt du serveur"""
    logger.info("Nettoyage final en cours...")
    for filename in os.listdir(TEMP_FOLDER):
        file_path = os.path.join(TEMP_FOLDER, filename)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Erreur lors du nettoyage de {file_path}: {e}")

atexit.register(cleanup_on_exit)

def is_valid_url(url):
    """Valide la structure de l'URL"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False

def validate_soundcloud_url(url, expected_type):
    """Validation spécifique pour les URLs SoundCloud"""
    try:
        parsed = urlparse(url)
        if 'soundcloud.com' not in parsed.netloc:
            return False
        
        path_parts = [p for p in parsed.path.split('/') if p]
        
        if expected_type == 'single':
            return len(path_parts) >= 2 and 'sets' not in path_parts
        elif expected_type == 'collection':
            return any(x in path_parts for x in ['sets', 'playlists', 'likes'])
        return False
    except Exception:
        return False

def sanitize_filename(filename):
    """Nettoie le nom de fichier pour le système de fichiers"""
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:200]  # Limite la longueur du nom de fichier

def download_soundcloud_collection(url, quality):
    """Télécharge une playlist SoundCloud et crée un ZIP"""
    temp_dir = os.path.join(TEMP_FOLDER, f"sc_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality
            }],
            'extract_flat': 'in_playlist',
            'ignoreerrors': True,
            'playlistend': 50,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            files = []
            for entry in info.get('entries', []):
                try:
                    base_path = os.path.splitext(ydl.prepare_filename(entry))[0]
                    mp3_path = f"{base_path}.mp3"
                    if os.path.exists(mp3_path):
                        files.append(mp3_path)
                except Exception as e:
                    logger.error(f"Erreur sur la piste: {e}")
            
            if not files:
                raise ValueError("Aucun fichier valide dans la playlist")
            
            zip_name = f"{sanitize_filename(info.get('title', 'playlist'))}.zip"
            zip_path = os.path.join(DOWNLOAD_FOLDER, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for file in files:
                    arcname = os.path.basename(file)
                    zipf.write(file, arcname)
            
            return zip_path
    except Exception as e:
        logger.error(f"Erreur lors du téléchargement de la playlist: {e}")
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def download_media(url, platform, quality=None):
    """Gestion principale du téléchargement"""
    temp_file = os.path.join(TEMP_FOLDER, f"dl_{int(time.time())}")
    
    try:
        with DOWNLOAD_LOCK:
            ACTIVE_DOWNLOADS.add(temp_file)

        if not is_valid_url(url):
            raise ValueError("URL invalide")

        ydl_opts = {
            'outtmpl': temp_file + '.%(ext)s',
            'logger': logger,
            'restrictfilenames': True,
            'noplaylist': True,
            'ignoreerrors': True,
            'retries': 10,
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls']
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }

        if platform == 'youtube':
            mode = request.form.get('mode', 'audio')
            if mode == 'audio':
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': quality or '192'
                    }]
                })
            else:
                ydl_opts.update({
                    'format': f'bestvideo[height<={quality or "720"}]+bestaudio/best[height<={quality or "720"}]',
                    'merge_output_format': 'mp4'
                })
        elif platform == 'soundcloud':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': quality or '192'
                }]
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # Trouve le fichier téléchargé
            downloaded_files = [f for f in os.listdir(TEMP_FOLDER) if f.startswith(os.path.basename(temp_file))]
            if not downloaded_files:
                raise FileNotFoundError("Aucun fichier téléchargé trouvé")
                
            temp_path = os.path.join(TEMP_FOLDER, downloaded_files[0])
            
            # Nom final du fichier
            final_filename = sanitize_filename(info.get('title', 'download')) + os.path.splitext(temp_path)[1]
            final_path = os.path.join(DOWNLOAD_FOLDER, final_filename)
            
            # Déplacement du fichier
            shutil.move(temp_path, final_path)
            
            return final_path
            
    except Exception as e:
        logger.error(f"Erreur de téléchargement: {e}")
        raise
    finally:
        with DOWNLOAD_LOCK:
            ACTIVE_DOWNLOADS.discard(temp_file)
        # Nettoyage des fichiers temporaires restants
        for f in os.listdir(TEMP_FOLDER):
            if f.startswith(os.path.basename(temp_file)):
                try:
                    os.remove(os.path.join(TEMP_FOLDER, f))
                except:
                    pass

@app.route("/")
def index():
    return render_template("index.html", messages=get_flashed_messages())

@app.route("/youtube", methods=["GET", "POST"])
def youtube():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            flash("URL manquante", "error")
            return redirect(url_for('youtube'))
        
        if not url.startswith(('http://', 'https://')):
            flash("URL invalide - doit commencer par http:// ou https://", "error")
            return redirect(url_for('youtube'))
        
        try:
            quality = request.form.get("quality", "192")
            filename = download_media(url, 'youtube', quality)
            flash("Téléchargement réussi!", "success")
            return redirect(url_for('downloaded', filename=os.path.basename(filename)))
        except Exception as e:
            flash(f"Erreur: {str(e)}", "error")
            logger.error(f"Erreur YouTube: {str(e)}")
    
    return render_template("youtube.html", messages=get_flashed_messages())

@app.route("/soundcloud", methods=["GET", "POST"])
def soundcloud():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            flash("URL manquante", "error")
            return redirect(url_for('soundcloud'))
        
        if not url.startswith(('http://', 'https://')):
            flash("URL invalide - doit commencer par http:// ou https://", "error")
            return redirect(url_for('soundcloud'))
        
        try:
            quality = request.form.get("quality", "192")
            content_type = request.form.get("content_type", "single")
            
            if not validate_soundcloud_url(url, content_type):
                raise ValueError("URL SoundCloud invalide pour ce type de contenu")
            
            if content_type == "collection":
                filename = download_soundcloud_collection(url, quality)
                flash("Playlist téléchargée avec succès!", "success")
            else:
                filename = download_media(url, 'soundcloud', quality)
                flash("Musique téléchargée avec succès!", "success")
            
            return redirect(url_for('downloaded', filename=os.path.basename(filename)))
        except Exception as e:
            flash(f"Erreur SoundCloud: {str(e)}", "error")
            logger.error(f"Erreur SoundCloud: {str(e)}")
    
    return render_template("soundcloud.html", messages=get_flashed_messages())

@app.route("/downloaded/<path:filename>")
def downloaded(filename):
    try:
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError("Fichier non disponible")
        
        # Délai de nettoyage adapté
        file_size = os.path.getsize(filepath) / (1024 * 1024)  # Taille en Mo
        cleanup_delay = min(max(10, int(file_size * 2)), 120)
        
        # Détection du type MIME
        if filename.lower().endswith('.mp3'):
            mimetype = 'audio/mpeg'
        elif filename.lower().endswith('.mp4'):
            mimetype = 'video/mp4'
        elif filename.lower().endswith('.zip'):
            mimetype = 'application/zip'
        else:
            mimetype = 'application/octet-stream'
        
        response = send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype=mimetype
        )
        
        # Planifie le nettoyage après l'envoi
        response.call_on_close(lambda: Timer(cleanup_delay, os.remove, args=(filepath,)).start())
        return response
    except Exception as e:
        flash(f"Erreur de téléchargement: {str(e)}", "error")
        return redirect(url_for('index'))

if __name__ == "__main__":
    try:
        logger.info("Serveur DualLoad démarré")
        logger.info(f"Accès: http://localhost:5000")
        app.run(host='0.0.0.0', port=5000, threaded=True)
    except Exception as e:
        logger.critical(f"Erreur critique: {str(e)}")