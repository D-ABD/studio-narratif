# Petit Studio Voix

Application web Django locale en français pour gérer une bibliothèque de voix, écrire des narrations en segments, les synthétiser avec XTTS v2 ou Chatterbox Multilingual CPU, écouter chaque segment, puis exporter un WAV complet et son fichier SRT synchronisé.

Les projets, échantillons vocaux, narrations et sous-titres sont stockés dans `media/`; les données sont dans `db.sqlite3`. Aucun compte ni service cloud n’est nécessaire à l’usage. Une connexion Internet est nécessaire à l’installation des paquets et au premier téléchargement des poids IA, ensuite mis en cache localement. Le code ne désactive pas la vérification TLS.

## Fonctionnalités

- Bibliothèque locale avec catégories Shiba, Homme, Femme et Autre, recherche et filtre.
- Import WAV/MP3 ou enregistrement microphone, écoute avant sauvegarde, normalisation locale en WAV mono 24 kHz.
- Consentement explicite avant l’enregistrement d’une voix clonée.
- Renommage, recatégorisation et suppression confirmée des voix.
- Projets avec plusieurs segments de texte modifiables, supprimables et réordonnables.
- Choix du moteur XTTS v2 ou Chatterbox Multilingual en C++/CPU, avec la même voix importée.
- Alignement forcé WhisperX sur l’audio réellement généré, quel que soit le moteur choisi.
- Réutilisation en mémoire du modèle XTTS, du modèle d’alignement WhisperX et des représentations de chaque voix pendant l’exécution du serveur.
- Cache disque par texte, empreinte SHA-256 de l’échantillon, moteur/modèle et langue ; les segments inchangés sont réutilisés.
- Pour Chatterbox, cache local permanent des représentations vocales du C++ pour les générations suivantes.
- Écoute et téléchargement de chaque segment, plus export WAV complet et SRT avec chronologie cumulée.
- Progression persistante en base, segment courant, temps écoulé, durées XTTS/WhisperX et état prêt/en cours/erreur.
- Interface responsive inspirée des captures fournies, sans dépendance à une police ou ressource distante.

## Prérequis

- Python 3.11 recommandé.
- FFmpeg dans le PATH pour normaliser les échantillons WAV/MP3/WebM. Sur macOS : `brew install ffmpeg`.
- Environ 8 à 10 Go libres pour les poids XTTS/WhisperX téléchargés au premier lancement. La synthèse CPU peut être lente ; une carte NVIDIA/CUDA accélère le traitement sur les systèmes compatibles.
- Chatterbox est optionnel. Son moteur C++ et ses modèles multilingues doivent être installés séparément ; prévois plusieurs Go libres. Il exige un échantillon vocal propre de plus de 5 secondes (10 à 15 secondes recommandées).

## Installation sur macOS

Ouvre Terminal dans le dossier du dépôt :

```bash
brew install ffmpeg
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -r requirements-ai.txt
python manage.py migrate
python scripts/check_ai_install.py
```

Le jeu de versions IA est volontairement fixé dans `requirements-ai.txt` pour Python 3.11 et les environnements Mac Intel qui nécessitent PyTorch 2.2.2. Installe-le dans un environnement virtuel neuf et vérifie que le terminal affiche bien `(.venv)` avant chaque commande. Si `pip` tente de compiler `llvmlite`, arrête l’installation et vérifie l’architecture et la version de Python utilisées ; ne lance pas de compilation LLVM manuelle.

Sur CPU, l’application limite PyTorch à quatre fils de calcul au maximum (ou au nombre de cœurs disponibles). Pour comparer les performances sur ta machine, définis `STUDIO_TORCH_THREADS` avant le démarrage, par exemple `export STUDIO_TORCH_THREADS=2`, puis relance le serveur. Essaie 1, 2 et 4 : le meilleur réglage dépend du processeur. Le modèle XTTS, l’alignement WhisperX et les latents vocaux sont conservés en mémoire tant que le processus Django reste actif. Les durées mesurées de synthèse et d’alignement sont affichées à la fin de la préparation.

Le script vérifie l’import de PyTorch et XTTS, `Xtts.inference` et la méthode autoregressive `GPT.generate` appelée pendant la synthèse. Pour vérifier aussi une synthèse complète, donne-lui un échantillon vocal autorisé :

```bash
python scripts/check_ai_install.py --sample /chemin/vers/mon-echantillon.wav
```

Cette commande charge XTTS v2 et télécharge ses poids si nécessaire. Elle écrit le WAV de test dans un dossier temporaire qui est supprimé à la fin.

### Installer le moteur Chatterbox Multilingual CPU (optionnel)

Chatterbox est fourni par le projet communautaire [`chatterbox.cpp`](https://github.com/gianni-cor/chatterbox.cpp), un port C++/ggml avec clonage vocal depuis un fichier de référence. Le modèle multilingue inclut le français. Cette installation est séparée de l’environnement `.venv` de l’application : les paquets utilisés pour convertir les poids ne remplacent donc pas PyTorch/XTTS dans l’application.

Installe les outils de compilation, puis construis le binaire :

```bash
brew install cmake git
mkdir -p ~/Applications
cd ~/Applications
git clone https://github.com/gianni-cor/chatterbox.cpp.git
cd chatterbox.cpp
./scripts/setup-ggml.sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target tts-cli -j2
```

Les convertisseurs de modèles utilisent un environnement Python séparé :

```bash
cd ~/Documents/GIT
git clone https://github.com/resemble-ai/chatterbox.git chatterbox-ref
cd chatterbox-ref
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install gguf safetensors scipy librosa resampy
cd ~/Applications/chatterbox.cpp
python scripts/convert-t3-mtl-to-gguf.py --quant q4_0 --out models/chatterbox-t3-mtl-q4_0.gguf
python scripts/convert-s3gen-to-gguf.py --variant mtl --quant q4_0 --out models/chatterbox-s3gen-mtl-q4_0.gguf
```

Active ensuite l’environnement principal de Petit Studio Voix, indique les trois chemins et démarre Django dans ce même Terminal :

```bash
cd ~/Documents/GIT/studio-narratif
source .venv/bin/activate
export CHATTERBOX_CPP_BIN="$HOME/Applications/chatterbox.cpp/build/tts-cli"
export CHATTERBOX_T3_MODEL="$HOME/Applications/chatterbox.cpp/models/chatterbox-t3-mtl-q4_0.gguf"
export CHATTERBOX_S3GEN_MODEL="$HOME/Applications/chatterbox.cpp/models/chatterbox-s3gen-mtl-q4_0.gguf"
export CHATTERBOX_CPP_THREADS=2
python manage.py runserver 127.0.0.1:8000
```

Dans un projet, choisis **Chatterbox Multilingual · CPU** dans le menu **Moteur vocal**. L’application garde XTTS v2 comme choix par défaut. Le premier lancement Chatterbox calcule et stocke localement le profil de la voix ; les générations suivantes le réutilisent. Si tu changes les fichiers de modèle Chatterbox, efface `media/chatterbox-voice-cache/` et modifie `CHATTERBOX_MODEL_ID` dans `studio/services.py` pour invalider aussi les audios du cache.

Le benchmark publié par le port C++ est réalisé sur d’autres processeurs et ne permet pas de garantir un temps sur un ancien Mac Intel. Fais d’abord un essai avec un segment français et compare le rendu vocal et les durées affichées avant de l’utiliser pour tout un projet.

## Lancement

Dans le même environnement virtuel :

```bash
python manage.py runserver 127.0.0.1:8000
```

Ouvre ensuite <http://127.0.0.1:8000/>. Le serveur de développement est prévu pour l’accès local ; ne l’expose pas directement sur Internet.

## Tests et contrôles

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Les tests Django couvrent l’autorisation et la gestion des voix, les segments et leur réorganisation, la sélection des moteurs, les clés de cache séparées par moteur, le cache du profil Chatterbox, l’alignement sans horodatages de secours, l’assemblage WAV et le calcul des offsets SRT. Le test d’import XTTS est automatiquement ignoré si les dépendances IA ne sont pas installées ; le script `check_ai_install.py` fournit le contrôle dédié. Chatterbox est testé avec un faux binaire dans les tests Django ; sa compilation et sa génération réelle nécessitent les modèles décrits ci-dessus.

## Utilisation

1. Ajoute une voix WAV/MP3 ou enregistre-la avec le microphone ; écoute-la avant de la sauvegarder et confirme les droits d’utilisation.
2. Crée un projet de narration, écris tes textes en segments et choisis une voix enregistrée.
3. Choisis XTTS v2 ou Chatterbox dans **Moteur vocal**, puis clique sur **Préparer les segments**. Le moteur crée les audios et WhisperX aligne chaque texte sur son audio. Les segments déjà prêts et inchangés sont réutilisés pour le même moteur.
4. Écoute ou télécharge chaque WAV/SRT séparément, puis récupère la narration complète et son SRT.

Si WhisperX échoue à aligner un segment, le projet passe en erreur avec le détail retourné ; aucun horodatage approximatif n’est créé.

## Licence et usage des voix

XTTS v2 est soumis à sa licence de modèle Coqui. Chatterbox.cpp et les modèles Chatterbox indiquent une licence MIT ; consulte les licences des poids avant un usage commercial. Enregistre uniquement ta voix ou une voix dont tu as obtenu l’autorisation explicite. L’application enregistre la confirmation de l’utilisateur mais ne peut pas vérifier les droits à sa place.
