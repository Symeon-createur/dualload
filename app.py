from flask import Flask, render_template, request, send_file, redirect, url_for, flash, get_flashed_messages, session, abort
import os
import yt_dlp
from urllib.parse import urlparse
import logging
import zipfile
import shutil
import re
import time
from datetime import datetime, timedelta
import atexit
import secrets
from functools import wraps
from collections import defaultdict
import datetime

# Initialisation de l'application
app = Flask(__name__, static_url_path='/static')

# Configuration sécurité
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(minutes=30)

# Configuration admin
ADMIN_PANEL = f"admin_{secrets.token_hex(8)}.html"
ADMIN_PASSWORD = "Mot2Passe"  # À modifier impérativement

# Configuration des dossiers
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

# Dictionnaire pour stocker les IPs connectées
connected_ips = defaultdict(lambda: {'last_activity': datetime.datetime.now()})
banned_ips = set()

# ==============================================
# Décorateurs et fonctions utilitaires
# ==============================================

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

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
            return (any(x in path_parts for x in ['sets', 'playlists', 'likes']) or \
                   (len(path_parts) == 1 and not any(x in path_parts for x in ['stream', 'tracks'])))
        return False
    except Exception:
        return False

def sanitize_filename(filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:200]

# ==============================================
# Middleware pour tracker les IPs
# ==============================================

@app.before_request
def track_activity():
    # Récupération de la vraie IP client (fonctionne derrière proxy)
    if request.headers.getlist("X-Forwarded-For"):
        client_ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    else:
        client_ip = request.remote_addr

    # Protection contre les IPs bannies
    if client_ip in banned_ips:
        abort(403, description="Accès refusé - IP bannie")
    
    # Tracking des activités admin
    if request.path.startswith('/admin'):
        connected_ips[client_ip] = {
            'last_activity': datetime.datetime.now(),
            'user_agent': request.headers.get('User-Agent', 'Inconnu'),
            'requests': connected_ips.get(client_ip, {}).get('requests', 0) + 1
        }
        logger.debug(f"Activité enregistrée pour IP: {client_ip}")

# ==============================================
# Fonctions de téléchargement
# ==============================================

def download_media(url, platform, quality=None):
    temp_file = os.path.join(TEMP_FOLDER, f"dl_{int(time.time())}")
    
    try:
        if platform == 'youtube':
            opts = {
                'format': f'bestvideo[height<={quality or "2160"}]+bestaudio/best',
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

# ==============================================
# Routes principales
# ==============================================

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
                valid_qualities = ['360', '480', '720', '1080', '1440', '2160']
                if quality not in valid_qualities:
                    quality = '1080'
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

# ==============================================
# Routes Admin
# ==============================================

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['is_admin'] = True
            session.permanent = True
            return redirect(url_for('admin_panel'))
        else:
            flash('Mot de passe incorrect', 'error')
    return render_template('admin_login.html')

@app.route('/admin')
@admin_required
def admin_panel():
    stats = {
        'downloads': len(os.listdir(DOWNLOAD_FOLDER)),
        'space_used': f"{sum(os.path.getsize(os.path.join(DOWNLOAD_FOLDER, f)) for f in os.listdir(DOWNLOAD_FOLDER)) / (1024*1024):.2f} MB",
        'last_downloads': sorted(
            [{
                'name': f,
                'date': datetime.fromtimestamp(os.path.getmtime(os.path.join(DOWNLOAD_FOLDER, f))).strftime('%Y-%m-%d %H:%M:%S'),
                'size': f"{os.path.getsize(os.path.join(DOWNLOAD_FOLDER, f)) / 1024:.1f} KB"
            } for f in os.listdir(DOWNLOAD_FOLDER)],
            key=lambda x: x['date'],
            reverse=True
        )[:10]
    }
    return render_template(ADMIN_PANEL, stats=stats, connected_ips=connected_ips, banned_ips=banned_ips)

@app.route('/admin/ban/<ip>')
@admin_required
def ban_ip(ip):
    banned_ips.add(ip)
    if ip in connected_ips:
        del connected_ips[ip]
    flash(f'IP {ip} bannie avec succès', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/unban/<ip>')
@admin_required
def unban_ip(ip):
    if ip in banned_ips:
        banned_ips.remove(ip)
    flash(f'IP {ip} débannie avec succès', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/kick/<ip>')
@admin_required
def kick_ip(ip):
    if ip in connected_ips:
        del connected_ips[ip]
    flash(f'IP {ip} kickée avec succès', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/cleanup')
@admin_required
def admin_cleanup():
    try:
        cleanup()
        flash('Nettoyage effectué avec succès', 'success')
    except Exception as e:
        flash(f'Erreur lors du nettoyage: {str(e)}', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    flash('Vous avez été déconnecté', 'success')
    return redirect(url_for('index'))

# ==============================================
# Fonctions d'initialisation
# ==============================================

def create_admin_template():
    global ADMIN_PANEL
    
    # Supprimer les anciens fichiers admin_*.html (sauf admin_login.html)
    for filename in os.listdir("templates"):
        if (filename.startswith("admin_") 
            and filename.endswith(".html")
            and filename != "admin_login.html"):
            try:
                os.remove(os.path.join("templates", filename))
                print(f"Supprimé : {filename}")
            except Exception as e:
                print(f"Erreur suppression {filename} : {e}")

    # Créer un nouveau fichier admin
    ADMIN_PANEL = f"admin_{secrets.token_hex(8)}.html"
    os.makedirs("templates", exist_ok=True)
    
    admin_template = r"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel Admin</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #FF3E41;
            --secondary: #2B2D42;
            --light: #F8F9FA;
            --dark: #212529;
        }
        
        body {
            font-family: 'Roboto', sans-serif;
            line-height: 1.6;
            color: var(--dark);
            background-color: #f5f5f5;
            margin: 0;
            padding: 20px;
        }
        
        .admin-container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 0 30px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        
        .admin-header {
            background: var(--secondary);
            color: white;
            padding: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .admin-title {
            margin: 0;
            font-weight: 500;
        }
        
        .btn {
            display: inline-block;
            padding: 10px 20px;
            border-radius: 5px;
            text-decoration: none;
            font-weight: 500;
            transition: all 0.3s;
            margin: 5px;
        }
        
        .btn-primary {
            background: var(--primary);
            color: white;
        }
        
        .btn-secondary {
            background: var(--secondary);
            color: white;
        }
        
        .btn-danger {
            background: #dc3545;
            color: white;
        }
        
        .btn-success {
            background: #28a745;
            color: white;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            padding: 20px;
        }
        
        .stat-card {
            background: var(--light);
            padding: 20px;
            border-radius: 8px;
            border-left: 4px solid var(--primary);
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }
        
        th, td {
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        
        th {
            background: var(--secondary);
            color: white;
        }
        
        tr:hover {
            background-color: rgba(0,0,0,0.02);
        }
        
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
        }
        
        .badge-success {
            background: #28a745;
            color: white;
        }
        
        .badge-warning {
            background: #ffc107;
            color: var(--dark);
        }
        
        .badge-danger {
            background: #dc3545;
            color: white;
        }
        
        .section {
            padding: 0 20px 20px;
        }
    </style>
</head>
<body>
    <div class="admin-container">
        <div class="admin-header">
            <h1 class="admin-title">👑 Panel Admin - DualLoad</h1>
            <a href="{{ url_for('admin_logout') }}" class="btn btn-secondary">Déconnexion</a>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <h3>Téléchargements</h3>
                <p>{{ stats['downloads'] }}</p>
            </div>
            <div class="stat-card">
                <h3>Espace utilisé</h3>
                <p>{{ stats['space_used'] }}</p>
            </div>
            <div class="stat-card">
                <h3>IPs connectées</h3>
                <p>{{ connected_ips|length }}</p>
            </div>
        </div>

        <div class="section">
            <h2>🔌 Utilisateurs connectés</h2>
            <table>
                <thead>
                    <tr>
                        <th>IP</th>
                        <th>User Agent</th>
                        <th>Requêtes</th>
                        <th>Dernière activité</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for ip, data in connected_ips.items() %}
                    <tr>
                        <td>{{ ip }}</td>
                        <td>{{ data.user_agent|truncate(50) }}</td>
                        <td>{{ data.requests }}</td>
                        <td>{{ data.last_activity.strftime('%H:%M:%S') }}</td>
                        <td>
                            <a href="{{ url_for('ban_ip', ip=ip) }}" class="btn btn-danger">Bannir</a>
                            <a href="{{ url_for('kick_ip', ip=ip) }}" class="btn btn-warning">Kick</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>🚫 IPs bannies</h2>
            {% if banned_ips %}
            <table>
                <thead>
                    <tr>
                        <th>IP</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for ip in banned_ips %}
                    <tr>
                        <td>{{ ip }}</td>
                        <td>
                            <a href="{{ url_for('unban_ip', ip=ip) }}" class="btn btn-success">Débannir</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p>Aucune IP bannie actuellement</p>
            {% endif %}
        </div>

        <div class="section">
            <h2>🗑️ Nettoyage</h2>
            <a href="{{ url_for('admin_cleanup') }}" class="btn btn-danger"
               onclick="return confirm('Vider tous les fichiers temporaires?')">
               Nettoyer les fichiers temporaires
            </a>
        </div>
    </div>
</body>
</html>"""
    with open(f"templates/{ADMIN_PANEL}", "w", encoding='utf-8') as f:
        f.write(admin_template)


@app.route('/')
def home():
    return "Application démarrée avec succès !"


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

# Initialisation au démarrage
create_admin_template()
atexit.register(cleanup)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)