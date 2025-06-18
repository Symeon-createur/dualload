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
import datetime
import psutil
from threading import Thread

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

# Dictionnaire pour stocker les visiteurs en temps réel
live_visitors = defaultdict(lambda: {
    'last_activity': datetime.datetime.now(),
    'user_agent': '',
    'requests': 0,
    'banned': False
})

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
# Middleware pour tracker les visiteurs
# ==============================================

@app.before_request
def track_activity():
    if request.headers.getlist("X-Forwarded-For"):
        client_ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
    else:
        client_ip = request.remote_addr

    if live_visitors[client_ip]['banned']:
        abort(403, description="Accès refusé - IP bannie")
    
    live_visitors[client_ip] = {
        'last_activity': datetime.datetime.now(),
        'user_agent': request.headers.get('User-Agent', 'Inconnu'),
        'requests': live_visitors.get(client_ip, {}).get('requests', 0) + 1,
        'banned': False
    }

# ==============================================
# Fonctions de téléchargement
# ==============================================

def download_media(url, platform, quality=None):
    temp_file = os.path.join(TEMP_FOLDER, f"dl_{int(time.time())}")
    
    try:
        if platform == 'youtube':
            if request.form.get("mode") == "audio":
                opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': temp_file + '.%(ext)s',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': quality or '192',
                    }],
                    'verbose': True,
                    'force_ipv4': True
                }
            else:
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
    return render_template(ADMIN_PANEL)

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
    if ip in live_visitors:
        live_visitors[ip]['banned'] = True
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error', 'message': 'IP non trouvée'}), 404

@app.route('/admin/api/unban/<ip>', methods=['POST'])
@admin_required
def admin_api_unban(ip):
    if ip in live_visitors:
        live_visitors[ip]['banned'] = False
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error', 'message': 'IP non trouvée'}), 404

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
    
    # Créer le dossier templates s'il n'existe pas
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    os.makedirs(templates_dir, exist_ok=True)

    # Supprimer les anciens fichiers admin_*.html
    for filename in os.listdir(templates_dir):
        if (filename.startswith("admin_") 
            and filename.endswith(".html")
            and filename != "admin_login.html"):
            try:
                os.remove(os.path.join(templates_dir, filename))
            except Exception as e:
                logger.error(f"Erreur suppression {filename}: {e}")

    # Créer un nouveau fichier admin
    ADMIN_PANEL = f"admin_{secrets.token_hex(8)}.html"
    admin_path = os.path.join(templates_dir, ADMIN_PANEL)
    os.makedirs("templates", exist_ok=True)
    
    admin_template = r"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel Admin - Temps Réel</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        .card { margin-bottom: 20px; }
        .visitor-table { max-height: 400px; overflow-y: auto; }
        .stat-card { border-left: 4px solid #4e73df; }
        .banned { background-color: #ffe6e6; }
        .chart-container { position: relative; height: 300px; width: 100%; }
    </style>
</head>
<body>
    <div class="container-fluid mt-4">
        <div class="d-sm-flex align-items-center justify-content-between mb-4">
            <h1 class="h3 mb-0 text-gray-800">Dashboard Admin - Temps Réel</h1>
            <a href="{{ url_for('admin_logout') }}" class="btn btn-danger">Déconnexion</a>
        </div>

        <div class="row">
            <!-- Visiteurs actifs -->
            <div class="col-xl-3 col-md-6 mb-4">
                <div class="card stat-card shadow h-100 py-2">
                    <div class="card-body">
                        <div class="row no-gutters align-items-center">
                            <div class="col mr-2">
                                <div class="text-xs font-weight-bold text-primary text-uppercase mb-1">
                                    Visiteurs actifs</div>
                                <div class="h5 mb-0 font-weight-bold text-gray-800" id="visitors-count">0</div>
                            </div>
                            <div class="col-auto">
                                <i class="fas fa-users fa-2x text-gray-300"></i>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- CPU Usage -->
            <div class="col-xl-3 col-md-6 mb-4">
                <div class="card stat-card shadow h-100 py-2">
                    <div class="card-body">
                        <div class="row no-gutters align-items-center">
                            <div class="col mr-2">
                                <div class="text-xs font-weight-bold text-success text-uppercase mb-1">
                                    CPU Usage</div>
                                <div class="h5 mb-0 font-weight-bold text-gray-800" id="cpu-usage">0%</div>
                            </div>
                            <div class="col-auto">
                                <i class="fas fa-microchip fa-2x text-gray-300"></i>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Memory Usage -->
            <div class="col-xl-3 col-md-6 mb-4">
                <div class="card stat-card shadow h-100 py-2">
                    <div class="card-body">
                        <div class="row no-gutters align-items-center">
                            <div class="col mr-2">
                                <div class="text-xs font-weight-bold text-info text-uppercase mb-1">
                                    Memory Usage</div>
                                <div class="h5 mb-0 font-weight-bold text-gray-800" id="memory-usage">0%</div>
                            </div>
                            <div class="col-auto">
                                <i class="fas fa-memory fa-2x text-gray-300"></i>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Disk Usage -->
            <div class="col-xl-3 col-md-6 mb-4">
                <div class="card stat-card shadow h-100 py-2">
                    <div class="card-body">
                        <div class="row no-gutters align-items-center">
                            <div class="col mr-2">
                                <div class="text-xs font-weight-bold text-warning text-uppercase mb-1">
                                    Disk Usage</div>
                                <div class="h5 mb-0 font-weight-bold text-gray-800" id="disk-usage">0%</div>
                            </div>
                            <div class="col-auto">
                                <i class="fas fa-hdd fa-2x text-gray-300"></i>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="row">
            <!-- Graphique des performances -->
            <div class="col-lg-6 mb-4">
                <div class="card shadow">
                    <div class="card-header py-3">
                        <h6 class="m-0 font-weight-bold text-primary">Performances du serveur</h6>
                    </div>
                    <div class="card-body">
                        <div class="chart-container">
                            <canvas id="performanceChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Graphique réseau -->
            <div class="col-lg-6 mb-4">
                <div class="card shadow">
                    <div class="card-header py-3">
                        <h6 class="m-0 font-weight-bold text-primary">Activité réseau (KB)</h6>
                    </div>
                    <div class="card-body">
                        <div class="chart-container">
                            <canvas id="networkChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Tableau des visiteurs -->
        <div class="card shadow mb-4">
            <div class="card-header py-3">
                <h6 class="m-0 font-weight-bold text-primary">Visiteurs en temps réel</h6>
            </div>
            <div class="card-body visitor-table">
                <div class="table-responsive">
                    <table class="table table-bordered" id="visitors-table">
                        <thead class="thead-light">
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
    </div>

    <script>
        // Configuration des graphiques
        const performanceCtx = document.getElementById('performanceChart').getContext('2d');
        const cpuChart = new Chart(performanceCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'CPU %',
                        data: [],
                        borderColor: 'rgb(75, 192, 192)',
                        backgroundColor: 'rgba(75, 192, 192, 0.1)',
                        borderWidth: 2,
                        tension: 0.1,
                        fill: true
                    },
                    {
                        label: 'Mémoire %',
                        data: [],
                        borderColor: 'rgb(54, 162, 235)',
                        backgroundColor: 'rgba(54, 162, 235, 0.1)',
                        borderWidth: 2,
                        tension: 0.1,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100
                    }
                }
            }
        });

        const networkCtx = document.getElementById('networkChart').getContext('2d');
        const networkChart = new Chart(networkCtx, {
            type: 'bar',
            data: {
                labels: ['Upload', 'Download'],
                datasets: [{
                    label: 'KB',
                    data: [0, 0],
                    backgroundColor: [
                        'rgba(255, 99, 132, 0.7)',
                        'rgba(54, 162, 235, 0.7)'
                    ],
                    borderColor: [
                        'rgba(255, 99, 132, 1)',
                        'rgba(54, 162, 235, 1)'
                    ],
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true
                    }
                }
            }
        });

        // Mise à jour des données en temps réel
        function updateStats() {
            fetch('/admin/api/stats')
                .then(response => response.json())
                .then(data => {
                    // Mettre à jour les compteurs
                    document.getElementById('visitors-count').textContent = data.visitors;
                    document.getElementById('cpu-usage').textContent = data.system.cpu.toFixed(1) + '%';
                    document.getElementById('memory-usage').textContent = data.system.memory.toFixed(1) + '%';
                    document.getElementById('disk-usage').textContent = data.system.disk.toFixed(1) + '%';

                    // Mettre à jour les graphiques
                    const now = new Date().toLocaleTimeString();
                    cpuChart.data.labels.push(now);
                    cpuChart.data.datasets[0].data.push(data.system.cpu);
                    cpuChart.data.datasets[1].data.push(data.system.memory);
                    if (cpuChart.data.labels.length > 15) {
                        cpuChart.data.labels.shift();
                        cpuChart.data.datasets[0].data.shift();
                        cpuChart.data.datasets[1].data.shift();
                    }
                    cpuChart.update();

                    networkChart.data.datasets[0].data = [
                        data.system.network.sent / 1024,
                        data.system.network.recv / 1024
                    ];
                    networkChart.update();

                    // Mettre à jour la table des visiteurs
                    const tbody = document.getElementById('visitors-body');
                    tbody.innerHTML = '';
                    for (const [ip, info] of Object.entries(data.ips)) {
                        const row = document.createElement('tr');
                        if (info.banned) row.className = 'banned';
                        
                        const lastActivity = new Date(info.last_activity);
                        const now = new Date();
                        const diffMinutes = Math.floor((now - lastActivity) / 60000);
                        
                        row.innerHTML = `
                            <td>${ip}</td>
                            <td title="${info.user_agent}">${info.user_agent.substring(0, 50)}${info.user_agent.length > 50 ? '...' : ''}</td>
                            <td>${info.requests}</td>
                            <td>${diffMinutes} minute(s) ago</td>
                            <td>
                                ${info.banned ? 
                                    `<button class="btn btn-sm btn-success unban-btn" data-ip="${ip}"><i class="fas fa-unlock"></i> Unban</button>` : 
                                    `<button class="btn btn-sm btn-danger ban-btn" data-ip="${ip}"><i class="fas fa-ban"></i> Ban</button>`}
                            </td>
                        `;
                        tbody.appendChild(row);
                    }

                    // Ajouter les événements aux boutons
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

        // Actualiser toutes les 2 secondes
        setInterval(updateStats, 2000);
        updateStats(); // Première mise à jour
    </script>
</body>
</html>"""
    
    with open(admin_path, "w", encoding='utf-8') as f:
        f.write(admin_template)
    logger.info(f"Template admin créé : {admin_path}")

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