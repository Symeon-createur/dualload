from flask import Flask, render_template, request, send_file, redirect, url_for, flash, get_flashed_messages, session, abort, jsonify
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
import psutil
from threading import Thread, Lock
from werkzeug.security import generate_password_hash, check_password_hash

# Initialisation de l'application
app = Flask(__name__, static_url_path='/static')

# Configuration sécurité
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(minutes=30)
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Configuration admin
ADMIN_PANEL = f"admin_{secrets.token_hex(8)}.html"
ADMIN_PASSWORD = generate_password_hash("VotreMot2PasseComplexe!123")  # À modifier

# Configuration des dossiers
DOWNLOAD_FOLDER = os.path.abspath("downloads")
TEMP_FOLDER = os.path.abspath("temp_downloads")
LOG_FOLDER = os.path.abspath("logs")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_FOLDER, 'app.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Dictionnaire pour stocker les visiteurs en temps réel
live_visitors = defaultdict(lambda: {
    'last_activity': datetime.now(),
    'user_agent': '',
    'requests': 0,
    'banned': False
})
visitors_lock = Lock()

# Statistiques système
system_stats = {
    'cpu': 0,
    'memory': 0,
    'disk': 0,
    'network': {'sent': 0, 'recv': 0}
}

# Thread pour mettre à jour les stats système
def update_system_stats():
    while True:
        system_stats['cpu'] = psutil.cpu_percent()
        system_stats['memory'] = psutil.virtual_memory().percent
        system_stats['disk'] = psutil.disk_usage('/').percent
        net_io = psutil.net_io_counters()
        system_stats['network'] = {
            'sent': net_io.bytes_sent,
            'recv': net_io.bytes_recv
        }
        time.sleep(2)

stats_thread = Thread(target=update_system_stats)
stats_thread.daemon = True
stats_thread.start()

# ==============================================
# Décorateurs et fonctions utilitaires
# ==============================================

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not all([
            session.get('is_admin'),
            session.get('admin_ip') == get_client_ip(),
            session.get('_fresh')
        ]):
            session.clear()
            flash('Authentification admin requise', 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = get_client_ip()
        with visitors_lock:
            if live_visitors[client_ip].get('banned', False):
                abort(403, description="Accès refusé - IP bannie")
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
    # Garde les caractères alphanumériques, espaces, et certains caractères spéciaux
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:250]  # Augmentez la limite si nécessaire

def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    return request.remote_addr

# ==============================================
# Middleware pour tracker les visiteurs
# ==============================================

@app.before_request
def track_activity():
    client_ip = get_client_ip()
    with visitors_lock:
        if live_visitors[client_ip].get('banned', False):
            abort(403, description="Accès refusé - IP bannie")
        
        live_visitors[client_ip] = {
            'last_activity': datetime.now(),
            'user_agent': request.headers.get('User-Agent', 'Inconnu')[:200],
            'requests': live_visitors[client_ip].get('requests', 0) + 1,
            'banned': False
        }

# ==============================================
# Fonctions de téléchargement
# ==============================================

def download_media(url, platform, quality=None):
    temp_file = os.path.join(TEMP_FOLDER, f"dl_{int(time.time())}_{secrets.token_hex(4)}")
    
    try:
        ydl_opts = {
            'format': 'bestaudio/best' if request.form.get("mode") == "audio" else f'bestvideo[height<={quality or "1080"}]+bestaudio/best',
            'outtmpl': temp_file + '.%(ext)s',
            'verbose': False,
            'force_ipv4': True,
            'quiet': True,
            'no_warnings': True
        }

        if platform == 'youtube' and request.form.get("mode") == "audio":
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality or '192',
            }]
        elif platform == 'soundcloud':
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality or '320',
            }]
        else:
            ydl_opts['merge_output_format'] = 'mp4'

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            downloaded_files = [f for f in os.listdir(TEMP_FOLDER) if f.startswith(os.path.basename(temp_file))]
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
    temp_dir = os.path.join(TEMP_FOLDER, f"sc_{int(time.time())}_{secrets.token_hex(4)}")
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
            'playlistend': 50,
            'ignoreerrors': True,
            'verbose': False,
            'force_ipv4': True,
            'quiet': True,
            'no_warnings': True,
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
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
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
@rate_limit
def youtube():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        if not url:
            flash("URL manquante", "error")
            return redirect(url_for('youtube'))
        
        if not is_valid_url(url) or ('youtube.com' not in url and 'youtu.be' not in url):
            flash("URL YouTube invalide", "error")
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
@rate_limit
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
                flash("Playlist téléchargée avec succès!", "success")
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
        # Nouvelle validation plus permissive
        if not re.match(r'^[\w\-.()\[\] ]+$', filename) or any(x in filename for x in ['..', '/', '\\']):
            logger.error(f"Nom de fichier rejeté : {filename}")
            raise ValueError("Nom de fichier contient des caractères non autorisés")
            
        filepath = os.path.join(DOWNLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError("Fichier non disponible")
        
        ext = os.path.splitext(filename)[1].lower()
        mime_types = {
            '.mp3': 'audio/mpeg',
            '.mp4': 'video/mp4',
            '.zip': 'application/zip'
        }
        mimetype = mime_types.get(ext, 'application/octet-stream')
        
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
        password = request.form.get('password', '').strip()
        
        if check_password_hash(ADMIN_PASSWORD, password):
            session.clear()
            session['is_admin'] = True
            session['_fresh'] = True
            session['admin_ip'] = get_client_ip()
            session.permanent = True
            
            logger.info(f"Connexion admin réussie depuis {get_client_ip()}")
            return redirect(url_for('admin_panel'))
        
        flash('Mot de passe incorrect', 'error')
        logger.warning(f"Tentative de connexion admin échouée depuis {get_client_ip()}")
    
    return render_template('admin_login.html')

@app.route('/admin')
@admin_required
def admin_panel():
    try:
        downloads = []
        for f in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, f)
            if os.path.isfile(file_path):
                mtime = os.path.getmtime(file_path)
                downloads.append({
                    'name': f,
                    'date': datetime.fromtimestamp(mtime),
                    'size': os.path.getsize(file_path)
                })

        downloads.sort(key=lambda x: x['date'], reverse=True)
        
        return render_template(ADMIN_PANEL, 
                           stats={
                               'downloads': len(downloads),
                               'space_used': f"{sum(f['size'] for f in downloads) / (1024*1024):.2f} MB",
                               'last_downloads': downloads[:10]
                           },
                           system_stats=system_stats,
                           visitors=live_visitors)
    except Exception as e:
        logger.error(f"Erreur dans admin_panel: {str(e)}", exc_info=True)
        abort(500, description="Erreur interne du serveur")

@app.route('/admin/api/stats')
@admin_required
def admin_api_stats():
    return jsonify({
        'visitors': len(live_visitors),
        'system': system_stats,
        'ips': {ip: {
            'user_agent': data['user_agent'],
            'requests': data['requests'],
            'last_activity': data['last_activity'].isoformat(),
            'banned': data['banned']
        } for ip, data in live_visitors.items()}
    })

@app.route('/admin/api/ban/<ip>', methods=['POST'])
@admin_required
def admin_api_ban(ip):
    with visitors_lock:
        if ip in live_visitors:
            live_visitors[ip]['banned'] = True
            logger.info(f"IP bannie: {ip}")
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'message': 'IP non trouvée'}), 404

@app.route('/admin/api/unban/<ip>', methods=['POST'])
@admin_required
def admin_api_unban(ip):
    with visitors_lock:
        if ip in live_visitors:
            live_visitors[ip]['banned'] = False
            logger.info(f"IP débannie: {ip}")
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'message': 'IP non trouvée'}), 404

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Vous avez été déconnecté', 'success')
    return redirect(url_for('index'))

# ==============================================
# Fonctions d'initialisation
# ==============================================

def create_admin_template():
    global ADMIN_PANEL
    
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    os.makedirs(templates_dir, exist_ok=True)

    for filename in os.listdir(templates_dir):
        if (filename.startswith("admin_") and filename.endswith(".html") and filename != "admin_login.html"):
            try:
                os.remove(os.path.join(templates_dir, filename))
            except Exception as e:
                logger.error(f"Erreur suppression {filename}: {e}")

    ADMIN_PANEL = f"admin_{secrets.token_hex(8)}.html"
    admin_path = os.path.join(templates_dir, ADMIN_PANEL)
    
    admin_template = r"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Panel - DualLoad</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --primary: #4e73df;
            --secondary: #2c3e50;
            --success: #1cc88a;
            --info: #36b9cc;
            --warning: #f6c23e;
            --danger: #e74a3b;
        }
        
        body {
            background-color: #f8f9fc;
            font-family: 'Nunito', sans-serif;
        }
        
        .sidebar {
            background: linear-gradient(180deg, var(--primary) 10%, #224abe 100%);
            min-height: 100vh;
        }
        
        .card {
            border: none;
            border-radius: 0.35rem;
            box-shadow: 0 0.15rem 1.75rem 0 rgba(58, 59, 69, 0.15);
        }
        
        .stat-card {
            border-left: 0.25rem solid;
        }
        
        .stat-card.cpu {
            border-left-color: var(--info);
        }
        
        .stat-card.memory {
            border-left-color: var(--warning);
        }
        
        .stat-card.disk {
            border-left-color: var(--success);
        }
        
        .visitor-row.banned {
            background-color: #fee;
        }
        
        .file-icon {
            font-size: 1.5rem;
        }
        
        .mp3 { color: #4e73df; }
        .mp4 { color: #e74a3b; }
        .zip { color: #1cc88a; }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="row">
            <!-- Sidebar -->
            <div class="col-md-3 col-lg-2 d-md-block sidebar collapse bg-primary text-white">
                <div class="position-sticky pt-3">
                    <h4 class="text-center mb-4">
                        <i class="bi bi-shield-lock"></i> Admin Panel
                    </h4>
                    <ul class="nav flex-column">
                        <li class="nav-item">
                            <a class="nav-link active text-white" href="#">
                                <i class="bi bi-speedometer2 me-2"></i>Dashboard
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link text-white" href="#visitors">
                                <i class="bi bi-people me-2"></i>Visiteurs
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link text-white" href="#downloads">
                                <i class="bi bi-download me-2"></i>Téléchargements
                            </a>
                        </li>
                        <li class="nav-item mt-4">
                            <a class="nav-link text-white" href="{{ url_for('admin_logout') }}">
                                <i class="bi bi-box-arrow-right me-2"></i>Déconnexion
                            </a>
                        </li>
                    </ul>
                </div>
            </div>

            <!-- Main Content -->
            <main class="col-md-9 ms-sm-auto col-lg-10 px-md-4 py-4">
                <div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
                    <h1 class="h2">
                        <i class="bi bi-speedometer2"></i> Dashboard
                    </h1>
                    <div class="btn-toolbar mb-2 mb-md-0">
                        <span class="badge bg-primary">
                            <i class="bi bi-activity"></i> Temps réel
                        </span>
                    </div>
                </div>

                <!-- Stats Cards -->
                <div class="row mb-4">
                    <div class="col-xl-3 col-md-6 mb-4">
                        <div class="card stat-card h-100">
                            <div class="card-body">
                                <div class="row align-items-center">
                                    <div class="col">
                                        <h6 class="text-uppercase text-muted mb-2">
                                            Visiteurs
                                        </h6>
                                        <h2 class="mb-0" id="visitors-count">0</h2>
                                    </div>
                                    <div class="col-auto">
                                        <i class="bi bi-people fs-1 text-primary"></i>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="col-xl-3 col-md-6 mb-4">
                        <div class="card stat-card cpu h-100">
                            <div class="card-body">
                                <div class="row align-items-center">
                                    <div class="col">
                                        <h6 class="text-uppercase text-muted mb-2">
                                            CPU
                                        </h6>
                                        <h2 class="mb-0" id="cpu-usage">0%</h2>
                                    </div>
                                    <div class="col-auto">
                                        <i class="bi bi-cpu fs-1 text-info"></i>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="col-xl-3 col-md-6 mb-4">
                        <div class="card stat-card memory h-100">
                            <div class="card-body">
                                <div class="row align-items-center">
                                    <div class="col">
                                        <h6 class="text-uppercase text-muted mb-2">
                                            Mémoire
                                        </h6>
                                        <h2 class="mb-0" id="memory-usage">0%</h2>
                                    </div>
                                    <div class="col-auto">
                                        <i class="bi bi-memory fs-1 text-warning"></i>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="col-xl-3 col-md-6 mb-4">
                        <div class="card stat-card disk h-100">
                            <div class="card-body">
                                <div class="row align-items-center">
                                    <div class="col">
                                        <h6 class="text-uppercase text-muted mb-2">
                                            Disque
                                        </h6>
                                        <h2 class="mb-0" id="disk-usage">0%</h2>
                                    </div>
                                    <div class="col-auto">
                                        <i class="bi bi-hdd fs-1 text-success"></i>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Charts Row -->
                <div class="row mb-4">
                    <div class="col-lg-6 mb-4">
                        <div class="card">
                            <div class="card-header bg-primary text-white">
                                <i class="bi bi-graph-up me-2"></i> Performances du serveur
                            </div>
                            <div class="card-body">
                                <canvas id="performanceChart" height="200"></canvas>
                            </div>
                        </div>
                    </div>
                    <div class="col-lg-6 mb-4">
                        <div class="card">
                            <div class="card-header bg-primary text-white">
                                <i class="bi bi-network-chart me-2"></i> Activité réseau
                            </div>
                            <div class="card-body">
                                <canvas id="networkChart" height="200"></canvas>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Visitors Section -->
                <div class="card mb-4" id="visitors">
                    <div class="card-header bg-primary text-white">
                        <i class="bi bi-people-fill me-2"></i> Visiteurs en temps réel
                    </div>
                    <div class="card-body">
                        <div class="table-responsive">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th>IP</th>
                                        <th>User Agent</th>
                                        <th>Requêtes</th>
                                        <th>Dernière activité</th>
                                        <th>Actions</th>
                                    </tr>
                                </thead>
                                <tbody id="visitors-body">
                                    <!-- Rempli par JavaScript -->
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- Downloads Section -->
                <div class="card" id="downloads">
                    <div class="card-header bg-primary text-white">
                        <i class="bi bi-download me-2"></i> Derniers téléchargements
                    </div>
                    <div class="card-body">
                        <div class="table-responsive">
                            <table class="table table-hover">
                                <thead>
                                    <tr>
                                        <th>Fichier</th>
                                        <th>Taille</th>
                                        <th>Date</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for file in stats.last_downloads %}
                                    <tr>
                                        <td>
                                            {% if file.name.endswith('.mp3') %}
                                                <i class="bi bi-file-earmark-music file-icon mp3"></i>
                                            {% elif file.name.endswith('.mp4') %}
                                                <i class="bi bi-file-earmark-play file-icon mp4"></i>
                                            {% elif file.name.endswith('.zip') %}
                                                <i class="bi bi-file-earmark-zip file-icon zip"></i>
                                            {% else %}
                                                <i class="bi bi-file-earmark file-icon"></i>
                                            {% endif %}
                                            {{ file.name }}
                                        </td>
                                        <td>
                                            {% if file.size > 1024*1024 %}
                                                {{ (file.size/(1024*1024))|round(2) }} MB
                                            {% else %}
                                                {{ (file.size/1024)|round(2) }} KB
                                            {% endif %}
                                        </td>
                                        <td>{{ file.date.strftime('%d/%m/%Y %H:%M') }}</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </main>
        </div>
    </div>

    <script>
        // Graphique des performances
        const cpuCtx = document.getElementById('performanceChart').getContext('2d');
        const cpuChart = new Chart(cpuCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'CPU %',
                        data: [],
                        borderColor: '#36b9cc',
                        backgroundColor: 'rgba(54, 185, 204, 0.1)',
                        tension: 0.3,
                        fill: true
                    },
                    {
                        label: 'Mémoire %',
                        data: [],
                        borderColor: '#f6c23e',
                        backgroundColor: 'rgba(246, 194, 62, 0.1)',
                        tension: 0.3,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        position: 'top',
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100
                    }
                }
            }
        });

        // Graphique réseau
        const netCtx = document.getElementById('networkChart').getContext('2d');
        const netChart = new Chart(netCtx, {
            type: 'bar',
            data: {
                labels: ['Upload', 'Download'],
                datasets: [{
                    label: 'KB',
                    data: [0, 0],
                    backgroundColor: [
                        'rgba(231, 74, 59, 0.7)',
                        'rgba(28, 200, 138, 0.7)'
                    ],
                    borderColor: [
                        'rgba(231, 74, 59, 1)',
                        'rgba(28, 200, 138, 1)'
                    ],
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        display: false
                    }
                }
            }
        });

        // Mise à jour des données
        function updateStats() {
            fetch('/admin/api/stats')
                .then(res => res.json())
                .then(data => {
                    // Mettre à jour les cartes stats
                    document.getElementById('visitors-count').textContent = data.visitors;
                    document.getElementById('cpu-usage').textContent = data.system.cpu.toFixed(1) + '%';
                    document.getElementById('memory-usage').textContent = data.system.memory.toFixed(1) + '%';
                    document.getElementById('disk-usage').textContent = data.system.disk.toFixed(1) + '%';

                    // Mettre à jour les graphiques
                    const now = new Date().toLocaleTimeString();
                    cpuChart.data.labels.push(now);
                    cpuChart.data.datasets[0].data.push(data.system.cpu);
                    cpuChart.data.datasets[1].data.push(data.system.memory);
                    
                    if (cpuChart.data.labels.length > 10) {
                        cpuChart.data.labels.shift();
                        cpuChart.data.datasets.forEach(dataset => dataset.data.shift());
                    }
                    cpuChart.update();

                    netChart.data.datasets[0].data = [
                        data.system.network.sent / 1024,
                        data.system.network.recv / 1024
                    ];
                    netChart.update();

                    // Mettre à jour la table des visiteurs
                    const tbody = document.getElementById('visitors-body');
                    tbody.innerHTML = '';
                    
                    for (const [ip, info] of Object.entries(data.ips)) {
                        const row = document.createElement('tr');
                        if (info.banned) row.className = 'visitor-row banned';
                        else row.className = 'visitor-row';
                        
                        const lastActive = new Date(info.last_activity);
                        const now = new Date();
                        const diffMinutes = Math.floor((now - lastActive) / 60000);
                        
                        row.innerHTML = `
                            <td>${ip}</td>
                            <td><span title="${info.user_agent}">${info.user_agent.substring(0, 40)}${info.user_agent.length > 40 ? '...' : ''}</span></td>
                            <td>${info.requests}</td>
                            <td>${diffMinutes} min ago</td>
                            <td>
                                ${info.banned ? 
                                    `<button class="btn btn-sm btn-success unban-btn" data-ip="${ip}">
                                        <i class="bi bi-unlock"></i> Unban
                                    </button>` : 
                                    `<button class="btn btn-sm btn-danger ban-btn" data-ip="${ip}">
                                        <i class="bi bi-ban"></i> Ban
                                    </button>`}
                            </td>
                        `;
                        tbody.appendChild(row);
                    }

                    // Gestion des boutons ban/unban
                    document.querySelectorAll('.ban-btn').forEach(btn => {
                        btn.addEventListener('click', () => banIP(btn.dataset.ip));
                    });
                    document.querySelectorAll('.unban-btn').forEach(btn => {
                        btn.addEventListener('click', () => unbanIP(btn.dataset.ip));
                    });
                });
        }

        // Fonctions pour ban/unban
        function banIP(ip) {
            fetch(`/admin/api/ban/${ip}`, { method: 'POST' })
                .then(() => updateStats());
        }

        function unbanIP(ip) {
            fetch(`/admin/api/unban/${ip}`, { method: 'POST' })
                .then(() => updateStats());
        }

        // Actualisation automatique
        setInterval(updateStats, 2000);
        updateStats(); // Première mise à jour
    </script>
</body>
</html>"""
    
    with open(admin_path, "w", encoding='utf-8') as f:
        f.write(admin_template)
    logger.info(f"Template admin créé : {admin_path}")

def cleanup():
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

# Initialisation
create_admin_template()
atexit.register(cleanup)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)