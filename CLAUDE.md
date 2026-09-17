# EtsyDrop — Instructions permanentes pour Claude Code

Tu travailles sur EtsyDrop, un SaaS pour les dropshippers Etsy.
Lis ce fichier en entier avant chaque tâche. Il prime sur tout autre instruction.

---

## PROJET

- **Nom** : EtsyDrop
- **Utilisateur cible** : Vendeurs Etsy français qui font du dropshipping
- **Stack frontend** : HTML + Tailwind CDN + Alpine.js + Chart.js + Font Awesome 6
- **Stack backend** : Python FastAPI + Supabase (PostgreSQL)
- **Hosting** : Vercel (frontend) + Railway (backend)
- **Couleur accent** : #00C896
- **Langue UI** : Français (navigation, labels, menus, messages)
- **Langue fiches produit** : Anglais obligatoire — titres, descriptions, tags, variantes sont toujours en anglais car on vise le marché international (US, UK, AU, CA en priorité)
- **Boutique de démo** : Lumino Bijoux / contact@luminobijoux.fr
- **Marché cible** : International — les prix s'affichent en € ET en $ selon le pays

---

## RÈGLES DE CODE

- Ne jamais supprimer du code existant sauf si explicitement demandé
- Toujours garder Alpine.js x-data="app()" comme point d'entrée unique
- Les charts Chart.js sont initialisés dans la fonction init() avec $nextTick
- Un seul fichier HTML — ne pas séparer en fichiers CSS/JS distincts
- Commenter chaque section avec === NOM DE LA SECTION ===
- Données de démo toujours réalistes (vrais prix €, vrais noms français)

---

## DESIGN — RÈGLES ABSOLUES

### Typographie
- **Toujours importer deux polices** depuis Google Fonts :
  `Plus Jakarta Sans` pour les titres et KPIs
  `Inter` pour le corps de texte
- KPI chiffres : `text-3xl font-extrabold tracking-tight` + `font-variant-numeric: tabular-nums`
- Labels sections : `text-[11px] font-bold uppercase tracking-widest text-gray-400`
- Corps : `text-sm leading-relaxed`

### Couleurs
- Accent unique : #00C896 — jamais deux couleurs accent qui se concurrencent
- Erreurs : `rose-500` (#F43F5E), jamais `red-500`
- Avertissements : `amber-500` (#F59E0B)
- Canvas fond : #F8F9FB — jamais blanc pur #FFF
- Cards : #FFFFFF sur fond canvas
- Dark mode canvas : #0D0F12, cards : #181B22, borders : #252A35

### Espacement
- Padding des cards : toujours `p-5` ou `p-6`, jamais `p-3`
- Gap entre sections : `space-y-6`
- Border radius : cards = `rounded-xl`, boutons = `rounded-lg`, badges = `rounded-full`

### CSS global obligatoire (toujours inclure)
```css
* {
  transition: color 150ms, background-color 150ms,
              box-shadow 150ms, transform 150ms, opacity 150ms;
  transition-timing-function: cubic-bezier(0.4, 0, 0.2, 1);
}
button:active { transform: scale(0.97); }
.card-hover:hover {
  box-shadow: 0 8px 24px rgba(0,0,0,0.10);
  transform: translateY(-1px);
}
::selection { background: rgba(0,200,150,0.2); }
html { scroll-behavior: smooth; }
.fade-enter {
  animation: fadeSlideIn 0.2s ease-out;
}
@keyframes fadeSlideIn {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}
.skeleton {
  background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}
@keyframes shimmer {
  0%   { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

### KPI Cards — structure obligatoire
Chaque card de KPI doit avoir :
1. Label en uppercase tracking-widest gray-400 (11px)
2. Chiffre principal extrabold tabular-nums
3. Indicateur de tendance : flèche + % vs période précédente (vert si positif, rose si négatif)

### Charts Chart.js — règles strictes
- Palette personnalisée uniquement :
  `accent: '#00C896', ink: '#1B1F27', blue: '#4C6FFF', amber: '#F5A623', rose: '#F43F5E', purple: '#8B5CF6'`
- Lignes : `tension: 0.4`, `pointRadius: 0` (visible au hover seulement), `borderWidth: 2.5`, fill à 8% opacity
- Donuts : `cutout: '72%'`, `borderWidth: 0`
- Barres : `borderRadius: 6`, pas de grille sur l'axe X
- Toujours `maintainAspectRatio: false`, hauteur contrôlée par le div parent

### Modals
- Backdrop : `bg-black/60 backdrop-blur-sm`
- Fermeture : clic backdrop + touche Escape
- Header sticky avec bouton fermer
- Scrollbar custom dans le contenu

---

## FEATURES SIGNATURE — À PROPOSER ET IMPLÉMENTER

Ces fonctionnalités différencient EtsyDrop de tous les concurrents (Everbee, Alura, eRank).
Propose-les et implémente-les dès que c'est pertinent.

### 1. Command Palette (Ctrl+K)
Overlay centré avec backdrop blur. Input de recherche instantanée.
Navigation vers toutes les pages + ouverture de fiches produit par nom.
Clavier : flèches pour naviguer, Entrée pour valider, Escape pour fermer.
Style : fond #0D0F12, bordure accent subtile, icône + label + shortcut par résultat.

### 2. Live Revenue Ticker
Le KPI revenu s'anime en count-up (0 → valeur réelle) sur 1.5s au chargement.
Point vert animate-ping + badge "Live" à côté.
Toutes les 25-45 secondes : micro-vente simulée (+24 à +87€) avec flash vert sur la card.

### 3. Sparklines dans les tableaux
Colonne "7j" avec SVG inline 64×24px généré dynamiquement (pas de lib externe).
Polyline SVG calculée depuis un tableau de 7 valeurs.
Couleur accent si tendance haussière, rose-500 si baissière.

### 4. Flip Cards 3D pour les concurrents
Face recto : stats du concurrent.
Face verso (fond dégradé accent) : tes avantages comparatifs vs ce concurrent.
CSS : `perspective(1000px) rotateY(180deg)` sur 0.5s au hover.

### 5. Confetti sur milestone
Quand la marge calculée dépasse 40% → confettis canvas 2s.
CDN : `https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js`
Couleurs : #00C896, #ffffff, #1B1F27. Une seule fois par session (flag booléen).

### 6. Seasonal Overlay sur les graphiques
Plugin Chart.js custom qui colore les périodes clés :
Fête des mères (mai-juin) : zone rose rgba(244,63,94,0.08)
Noël (nov-déc) : zone violette rgba(139,92,246,0.08)
Tooltip au survol : "🌸 Fête des mères — +35% ventes moy."

### 7. Keyboard Shortcuts
Bouton "?" fixe en bas à droite (rond 40px).
Raccourcis réels implémentés : G+D=Dashboard, G+S=Sourcing, G+O=Commandes, T=Dark mode, K=Command palette.
Overlay de référence des raccourcis au clic sur "?".

### 8. Focus Mode
Double-clic sur n'importe quelle card de graphique → plein écran overlay.
Utile pour analyser un graphique en détail.

### 9. Listing Quality Score (LQS)
Gauge SVG demi-cercle 0-100. Couleur selon score : rouge <50, orange 50-70, vert >70.
Checklist en 3 catégories : Titre / Description / Tags.
Présent dans le module SEO ET dans le modal catalogue de chaque produit.

### 10. Margin Slider visuel
Dans le calculateur de marge : slider à deux handles (coût ↔ prix).
Zone entre les handles colorée en temps réel selon la marge obtenue.

---

## MODULES DU PROJET

1. Dashboard — Vue d'ensemble avec KPIs live
2. Catalogue — Fiches produits complètes avec modal (galerie, variantes, prix par pays, LQS)
3. Sourcing — Comparateur multi-fournisseurs (Mon catalogue, Eprolo, CJ, Printify, AliExpress, Zendrop)
4. Commandes — Fulfillment automatique Etsy
5. SEO & Mots-clés — LQS + keywords avec volume, ventes totales Etsy, score
6. Rank Tracker — Position Etsy par mot-clé
7. Analyse Concurrents — Watchlist flip cards + Shop Analyzer
8. Niche Finder — Détection niches sous-exploitées
9. Promotion — Planning Pinterest, TikTok, Instagram, Facebook
10. Analytics — Revenus, marges, trafic sur 12 mois
11. Coûts & Expédition — Calculateur marge + simulateur promotions
12. Taxes & Change — TVA par pays, frais de change 1.8%, douanes
13. Mockups IA — Génération via Stability AI
14. Conseils IA — Rapport Claude + chat interactif
15. Intégrations & Paramètres — APIs + abonnement Free/Pro 12€/mois

---

## FICHES PRODUIT — RÈGLES INTERNATIONALES

Les fiches produit (titres, descriptions, tags, noms de variantes) sont TOUJOURS en anglais.
L'UI autour (labels, boutons, menus) reste en français.

Exemples corrects :
- Titre : "Personalized Gold Necklace with Name — Custom Jewelry Gift for Her"
- Tags : "custom necklace", "name jewelry", "gold plated", "personalized gift", "birthday gift for her"
- Variantes : "Gold / Small / No engraving" — pas "Or / Petite / Sans gravure"
- Description : "Add a personal touch with this handcrafted name necklace..."

Le générateur de fiche (bouton "Générer fiche Etsy") produit toujours du contenu en anglais,
optimisé pour les recherches US/UK sur Etsy.

---

## CYBERSÉCURITÉ — RÈGLES DE BASE

Ces règles s'appliquent à tout le code produit, frontend et backend.

### Frontend
- Jamais de clés API, tokens ou secrets dans le code HTML/JS frontend
- Toutes les clés API passent par le backend (FastAPI) — le frontend appelle uniquement `/api/...`
- Inputs utilisateur toujours sanitisés avant affichage : utiliser textContent, pas innerHTML
- Pas de `eval()`, pas de `document.write()`
- Les URLs externes (liens Etsy, fournisseurs) s'ouvrent avec `rel="noopener noreferrer"`
- Les cookies de session doivent avoir les flags `HttpOnly` et `Secure` (géré côté backend)

### Backend (FastAPI)
- Les variables sensibles (.env) ne sont jamais loggées ni retournées dans les réponses API
- Validation des inputs avec Pydantic sur tous les endpoints (pas de données brutes en DB)
- CORS configuré strictement : autoriser uniquement les domaines connus (localhost + domaine Vercel)
- Rate limiting sur les endpoints publics (login, recherche) — max 60 req/min par IP
- Les tokens Etsy OAuth sont stockés en base de données, jamais dans le localStorage
- Toujours utiliser des requêtes paramétrées (pas de concaténation de strings en SQL)
- Headers de sécurité HTTP à ajouter sur FastAPI :
  `X-Content-Type-Options: nosniff`
  `X-Frame-Options: DENY`
  `Referrer-Policy: strict-origin-when-cross-origin`

### Ce qu'on ne fait PAS
- Pas d'authentification maison complexe — utiliser Supabase Auth qui gère tout
- Pas de stockage de mots de passe — connexion via Etsy OAuth uniquement
- Pas de logs contenant des données personnelles (emails, noms clients)

---

## CHECKLIST AVANT DE LIVRER

Vérifie ces points avant chaque réponse :
- [ ] Chaque bouton a un état hover ET un état active (scale 0.97)
- [ ] Chaque page a une animation fade-in au changement
- [ ] Les états vides sont designés (jamais de contenu vide sans illustration + CTA)
- [ ] Le dark mode fonctionne sur tous les nouveaux éléments
- [ ] Les KPI cards ont leur indicateur de tendance
- [ ] Les tableaux ont hover sur les lignes + état vide prévu
- [ ] Aucun fond blanc pur #FFF ni noir pur #000
- [ ] Les chiffres sont formatés à la française (8 432,50 €)
- [ ] Le fichier compile sans erreur JavaScript dans la console
- [ ] Aucune clé API visible dans le code frontend
- [ ] Les liens externes ont rel="noopener noreferrer"
- [ ] Les fiches produit (titres, tags, descriptions) sont en anglais
- [ ] Les inputs utilisateur utilisent textContent et non innerHTML
