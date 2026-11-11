# Workflow de développement et Filet de sécurité

Ce projet utilise GitHub Actions pour garantir que `main` reste toujours fonctionnelle.

## 🛑 Règle d'or
- Je ne code JAMAIS directement en local sur `main`.
- Toutes les modifications de code sont poussées par ChatGPT sur `main`.
- Mon ordinateur local est un simple miroir de GitHub.

## 🚀 Le cycle de travail quotidien
1. Je demande une modification à ChatGPT.
2. ChatGPT pousse sur `main`.
3. Je vérifie que le run CI (GitHub Actions -> Tests) est au vert ✅.
4. Je récupère le code en local : `git sync`
5. Je teste l'application en local.
6. Si tout est OK, je valide le lot en créant un tag : `git lottag vX.Y.Z-lotX`

## 🚑 En cas de problème (Rollback)
Si un lot casse l'application et que les tests échouent :
1. Aller sur GitHub -> onglet "Actions" -> "Rollback to tag".
2. Cliquer sur "Run workflow".
3. Entrer le tag cible (ex: `v0.1.0-lot1`) et cliquer sur "Run workflow".
4. Le robot crée un commit de revert et le pousse sur `main`.
5. En local, faire `git sync` pour récupérer l'état sauvegardé.

## 📁 Fichiers importants
- `.github/workflows/ci.yml` : Lance les tests automatiquement à chaque push.
- `.github/workflows/rollback.yml` : Le bouton de secours manuel pour revenir à un tag.
- `.env.example` : Liste des variables d'environnement nécessaires (sans les secrets).