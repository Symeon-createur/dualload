from flask import Flask, render_template, request, send_file, redirect, url_for, flash, get_flashed_messages
import os
import yt_dlp
from urllib.parse import urlparse
import logging
import zipfile
import shutil
import re
import time
from datetime import datetime
import atexit

app = Flask(__name__, static_url_path='/static')
app.secret_key = os.urandom(24)

# Configuration
DOWNLOAD_FOLDER = os.path.abspath("downloads")
TEMP_FOLDER = os.path.abspath("temp_downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Configuration du logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False

def validate_soundcloud_url(url, expected_type):
    try:
        parsed = urlparse(url)
        if 'soundcloud.com' not in parsed.netloc:
            return False
        
        path_parts = [p for p in parsed.path.split('/') if p]
        
        if expected_type == 'single':
            return len(path_parts) >= 2 and 'sets' not in path_parts
        elif expected_type == 'collection':
            return (any(x in path_parts for x in ['sets', 'playlists', 'likes']) or 
                   (len(path_parts) == 1 and not any(x in path_parts for x in ['stream', 'tracks'])))
        return False
    except Exception:
        return False

def sanitize_filename(filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:200]

def download_media(url, platform, quality=None):
    temp_file = os.path.join(TEMP_FOLDER, f"dl_{int(time.time())}")
    
    try:
        if platform == 'youtube':
            opts = {
                'format': f'bestvideo[height<={quality or "1080"}]+bestaudio/best',
                'outtmpl': temp_file + '.%(ext)s',
                'merge_output_format': 'mp4',
                'verbose': True,
                'force_ipv4': True
            }
        elif platform == 'soundcloud':
            opts = {
                'format': 'bestaudio/best',
                'outtmpl': temp_file + '.%(ext)s',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': quality or '320',
                }],
                'verbose': True,
                'force_ipv4': True
            }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            downloaded_files = [f for f in os.listdir(TEMP_FOLDER) 
                             if f.startswith(os.path.basename(temp_file))]
            
            if not downloaded_files:
                raise FileNotFoundError("Aucun fichier téléchargé")
                
            temp_path = os.path.join(TEMP_FOLDER, downloaded_files[0])
            final_filename = f"{sanitize_filename(info.get('title', 'download'))}{os.path.splitext(temp_path)[1]}"
            final_path = os.path.join(DOWNLOAD_FOLDER, final_filename)
            
            shutil.move(temp_path, final_path)
            return final_path
            
    except Exception as e:
        logger.error(f"Erreur de téléchargement: {str(e)}", exc_info=True)
        raise
    finally:
        for f in os.listdir(TEMP_FOLDER):
            if f.startswith(os.path.basename(temp_file)):
                try:
                    os.remove(os.path.join(TEMP_FOLDER, f))
                except Exception as e:
                    logger.error(f"Erreur suppression fichier temporaire: {str(e)}")

def download_soundcloud_collection(url):
    temp_dir = os.path.join(TEMP_FOLDER, f"sc_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
            'extract_flat': False,
            'playlistend': 100,
            'ignoreerrors': True,
            'verbose': True,
            'force_ipv4': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
            }
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            if not info.get('entries'):
                raise ValueError("Aucune piste trouvée dans la collection")
            
            time.sleep(2)
            
            files = [f for f in os.listdir(temp_dir) if f.endswith('.mp3')]
            if not files:
                raise ValueError("Aucun fichier audio téléchargé")
            
            zip_name = f"{sanitize_filename(info.get('title', 'soundcloud_collection'))}.zip"
            zip_path = os.path.join(DOWNLOAD_FOLDER, zip_name)
            
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for file in files:
                    file_path = os.path.join(temp_dir, file)
                    zipf.write(file_path, arcname=file)
            
            return zip_path
            
    except Exception as e:
        logger.error(f"Erreur: {str(e)}", exc_info=True)
        raise ValueError(f"Échec du téléchargement: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

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
        
        try:
            quality = request.form.get("quality", "1080")
            mode = request.form.get("mode", "video")
            
            if mode == "audio":
                filename = download_media(url, 'youtube', '192')
            else:
                filename = download_media(url, 'youtube', quality)
                
            flash("Téléchargement réussi!", "success")
            return redirect(url_for('downloaded', filename=os.path.basename(filename)))
        except Exception as e:
            flash(f"Erreur: {str(e)}", "error")
            logger.error(f"Erreur YouTube: {str(e)}", exc_info=True)
    
    return render_template("youtube.html", messages=get_flashed_messages())

@app.route("/soundcloud", methods=["GET", "POST"])
def soundcloud():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            flash("URL manquante", "error")
            return redirect(url_for('soundcloud'))
        
        try:
            quality = request.form.get("quality", "320")
            content_type = request.form.get("content_type", "single")
            
            if not validate_soundcloud_url(url, content_type):
                flash("URL SoundCloud invalide pour ce type de contenu", "error")
                return redirect(url_for('soundcloud'))
            
            if content_type == "collection":
                filename = download_soundcloud_collection(url)
                flash("Playlist/likes téléchargés dans le ZIP!", "success")
            else:
                filename = download_media(url, 'soundcloud', quality)
                flash("Titre téléchargé avec succès!", "success")
            
            return redirect(url_for('downloaded', filename=os.path.basename(filename)))
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:
            flash("Erreur technique. Vérifiez l'URL ou réessayez.", "error")
            logger.error(f"Erreur SoundCloud: {str(e)}", exc_info=True)
    
    return render_template("soundcloud.html", messages=get_flashed_messages())

@app.route("/downloaded/<path:filename>")
def downloaded(filename):
    try:
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError("Fichier non disponible")
        
        if filename.lower().endswith('.mp3'):
            mimetype = 'audio/mpeg'
        elif filename.lower().endswith('.mp4'):
            mimetype = 'video/mp4'
        elif filename.lower().endswith('.zip'):
            mimetype = 'application/zip'
        else:
            mimetype = 'application/octet-stream'
        
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype=mimetype
        )
    except Exception as e:
        flash(f"Erreur de téléchargement: {str(e)}", "error")
        logger.error(f"Erreur downloaded: {str(e)}", exc_info=True)
        return redirect(url_for('index'))

def cleanup():
    """Nettoyage des dossiers temporaires"""
    for folder in [TEMP_FOLDER, DOWNLOAD_FOLDER]:
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logger.error(f"Échec suppression {file_path}: {e}")

# Nettoyage au démarrage
cleanup()
atexit.register(cleanup)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)