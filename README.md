# 🎵 DualLoad - Téléchargeur Multimédia Premium
**Plateforme complète de téléchargement YouTube & SoundCloud**  
🌐 [Site Officiel](https://dualload.online) | 📜 Licence Propriétaire

![DualLoad Banner](https://dualload.online/assets/banner.jpg)

## 🚀 Fonctionnalités Avancées
- **Téléchargement haute qualité** (jusqu'à 320kbps / 1080p)
- **Conversion intelligente** :
  - MP3 / MP4 / WAV / FLAC
  - Métadonnées préservées
- **Gestion des collections** :
  - Playlists complètes
  - Likes / favoris SoundCloud
- **File d’attente** avec reprise automatique

---

## 💻 Utilisation locale (Linux / macOS / Windows WSL)
L'application fonctionne **directement en local**. Aucun hébergement n’est nécessaire.

### Prérequis
- [Python 3.8+](https://www.python.org/downloads/)
- ✅ **[FFmpeg](https://ffmpeg.org/download.html)** (obligatoire pour fusionner audio + vidéo)

### Installation rapide
```bash
# Clonez le dépôt
git clone https://github.com/Symeon-createur/DualLoad.git
cd DualLoad

# Installez les dépendances Python
pip install -r requirements.txt

# Vérifiez que ffmpeg est installé
ffmpeg -version

# Lancez l'application
python app.py
