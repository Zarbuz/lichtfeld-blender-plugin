# Fork : budget de points initiaux relevé et ancres de profondeur garanties par vue

Fork de [MrNeRF/lichtfeld-blender-plugin](https://github.com/MrNeRF/lichtfeld-blender-plugin), branche
`seed-budget-depth-anchors`, partie de `main` au commit `f9daa08` (24/11/2026, après la 0.1.0-beta.2).
Version de l'extension : `0.1.0-beta.2.seedcap.1`. Elle remplace la 0.1.0-beta.2 installée (même identifiant
`lichtfeld_training`).

## Le problème

Avec **Depth supervision** activée, chaque vue doit voir au moins **256 points** du nuage de départ, et
la profondeur de chacun de ces points doit correspondre à sa carte de profondeur. Ces points servent d'ancres
pour caler la profondeur. Or ce nuage était plafonné à **250 000 points pour toute la scène**. Il était
ensuite réduit par un tirage spatial uniforme qui ne tient pas compte des vues.

Sur une grande capture intérieure, beaucoup de vues ne voient qu'une petite surface : un mur proche, un
couloir, une volée d'escalier. Le tirage spatial ne leur laisse que quelques dizaines de points. La capture
de 6 104 vues du Métaphone (Oignies) a ainsi échoué après 12 h de rendu, à l'étape d'initialisation :

```
View 463 has only 230 visible depth correspondences. Increase the initial point budget
or improve capture coverage (256 are required).
```

Un contrôle sur une vue sur vingt de cette capture a donné environ 9 % de vues sous les 256 points.
Relever le plafond était impossible : le maximum de l'interface et la validation du code étaient
tous deux bloqués à 250 000.

## Les modifications

1. **Plafond relevé** à 10 000 000 points (`capture_seed.MAX_SEED_POINTS`), dans la validation
   (`view_budget`) et dans le réglage *Initial point budget* de l'interface. Le curseur s'arrête à
   4 000 000, mais on peut saisir une valeur plus haute.
2. **Ancres réservées par vue** (`capture_seed.merge`, `reserved_indices`). Avant le tirage spatial,
   chaque vue garde jusqu'à **320** de ses propres échantillons (`ANCHORS_PER_VIEW`), répartis sur toute
   son image. Ces points sont pris sur ses pixels valides, hors discontinuités de profondeur, donc ils
   passent le contrôle d'ancrage. Le reste du budget est complété par le tirage spatial d'origine.
   - Le nombre total de points ne dépasse jamais le budget.
   - Le résultat est déterministe.
   - La réserve vaut `min(320, budget ÷ nombre de vues)` : un petit budget n'est jamais dépassé.
   - Quand tous les échantillons tiennent dans le budget, le résultat est le même qu'avant.
3. **Contrôle avant le rendu** (`render_worker.py`). Avec la supervision par profondeur, la capture refuse
   de démarrer si le budget est inférieur à 320 × le nombre de vues, au lieu d'échouer après le rendu.
   Le message donne la valeur minimale à saisir.
4. **Aide** du réglage mise à jour (`ui_help.py`). Le compte rendu d'initialisation note
   `reserved_per_view`.

Fichiers touchés : `capture_seed.py`, `render_worker.py`, `__init__.py` (une ligne), `ui_help.py`,
`blender_manifest.toml` (version), `tests/blender/test_capture_seed.py` (deux tests ajoutés).

## Utilisation

- Activer **Depth supervision**, puis régler **Initial point budget** à au moins **320 × le nombre de
  vues**, avec de la marge : par exemple 2 000 000 pour 6 104 vues (minimum 1 953 280).
- Chaque vue prélève alors `ceil(4 × budget ÷ vues)` échantillons, entre 256 et 4 096. Avec 2 000 000
  points et 6 104 vues, cela fait 1 311 échantillons par vue, dont 320 réservés.
- Le nuage de départ est plus gros, par exemple 2 M de points au lieu de 250 000. LichtFeld les accepte
  comme point de départ, et la stratégie MRNF reste plafonnée par *Max Gaussians*.
- **Une capture faite avec la 0.1.0-beta.2 ne peut pas être réutilisée pour la supervision par
  profondeur.** Ses échantillons par vue ont été prélevés avec l'ancien budget. Il faut relancer une
  capture avec cette version.

## Vérifications

- Tous les tests sans Blender de `tests/blender/test_*.py` passent, avant comme après la modification.
  Ils tournent avec `PYTHONPATH` pointant vers la roue `lichtfeld_training_protocol` décompressée.
- Deux tests ajoutés dans `test_capture_seed.py` :
  - `test_large_captures_accept_budgets_beyond_250k` : budgets, réserve et minimum pour 6 104 vues.
  - `test_every_view_keeps_its_own_anchors_under_a_global_budget` : 150 vues rapprochées de petites zones
    et une vue large. Avec l'ancien tirage, une vue tombe sous 256 points ; avec la réserve, chacune en
    garde au moins 320 et le résultat est identique d'une exécution à l'autre.
- Essai synthétique de bout en bout : couloir de 2 m × 40 m, 300 caméras, cartes de profondeur exactes,
  puis `samples`, `finalize` et `write_visible`. On a utilisé un petit `mathutils` de remplacement hors
  Blender.

  | Budget | Code d'origine | Ce fork |
  |---|---|---|
  | 100 000 points | échec (vue 38 : 187 correspondances) | réussite (au moins 529 ancres par vue) |
  | 20 000 points (< 320 × 300) | échec | refusé avant le rendu par le nouveau contrôle |

- Non vérifié ici : une capture complète dans Blender 5.3 avec entraînement LichtFeld jusqu'au bout.

## État en amont (25/11/2026)

- Aucune PR du dépôt d'origine (n° 1 à 63) ne touche `seed_points`, la limite de 250 000, `capture_seed` ou
  `DepthCoverageError`. Les PR qui n'apparaissent pas comme fusionnées dans `main` (n° 17-19, 35-39, 62 et
  63, ces deux dernières du 25/11) ont été vérifiées une à une par leurs différences.
- La page des issues n'a pas pu être lue depuis l'environnement de travail. À vérifier avant de proposer
  une PR.

## Installer cette version dans Blender

```
python tools/package_blender_training.py --output dist
```

Le paquet produit est `dist/lichtfeld_training-0.1.0-beta.2.seedcap.1.zip`. Pour l'installer :
*Edit ▸ Preferences ▸ Get Extensions ▸ ▾ ▸ Install from Disk…*, puis redémarrer Blender. Les réglages du
plugin, dont le chemin de LichtFeld Studio, sont conservés.
