# 🎵 DualLoad - Téléchargeur Multimédia Premium
**Plateforme complète de téléchargement YouTube & SoundCloud**  
🌐 [Site Officiel](https://dualload.online) | 📜 Licence Propriétaire

![DualLoad Banner](https://dualload.online/assets/banner.jpg)

## 🚀 Fonctionnalités Avancées
- **Téléchargement haute qualité** (jusqu'à 320kbps/4K)
- **Conversion intelligente** :
  - MP3/MP4/WAV/FLAC
  - Métadonnées préservées
- **Gestion des collections** :
  - Playlists complètes
  - Likes/FAV SoundCloud
- **Système de file d'attente** avec reprise automatique

## 💻 Installation (Linux/macOS/Windows WSL)
```bash
# Clonez le dépôt
git clone https://github.com/Symeon-createur/DualLoad.git
cd DualLoad

# Configurez l'environnement
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Installez les dépendances
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# Configuration initiale
cp config.example.py config.py
nano config.py  # Éditez selon vos besoins