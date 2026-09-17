"""
Notes de frais - application Flask pour association
=====================================================

Fonctionnalités :
- Comptes membres / trésorier
- Soumission d'une note de frais avec photo du justificatif
- Extraction automatique (OCR) du montant et de la date depuis la photo
- Validation / refus par le trésorier
- Export CSV des notes validées

Lancement local :
    pip install -r requirements.txt
    python app.py
Puis ouvrir http://127.0.0.1:5000

Le premier compte créé devient automatiquement trésorier.
"""

import csv
import io
import os
import re
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_from_directory, Response, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# STORAGE_DIR pointe vers le volume persistant Railway (ex: /data).
# En local (pas de volume), on retombe sur le dossier du projet.
STORAGE_DIR = os.environ.get("STORAGE_DIR", BASE_DIR)
os.makedirs(STORAGE_DIR, exist_ok=True)
DB_PATH = os.path.join(STORAGE_DIR, "notes_de_frais.db")
UPLOAD_DIR = os.path.join(STORAGE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "heic", "pdf"}
MAX_CONTENT_LENGTH = 12 * 1024 * 1024  # 12 Mo par photo

CATEGORIES = [
    "Fournitures", "Déplacement", "Repas", "Matériel sportif",
    "Communication", "Événement", "Adhésion / licence", "Autre",
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-moi-en-production")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# Base de données (sqlite3 brut, sans ORM)
# ----------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',  -- 'member' ou 'treasurer'
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            amount REAL,
            expense_date TEXT,
            photo_filename TEXT,
            ocr_raw_text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / reimbursed
            reviewer_comment TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        """
    )
    db.commit()
    db.close()


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Merci de te connecter.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def treasurer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "treasurer":
            flash("Accès réservé au trésorier.", "danger")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ----------------------------------------------------------------------------
# OCR : extraction du montant et de la date depuis la photo du justificatif
# ----------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"(\d{1,4}[.,]\d{2})")
TOTAL_KEYWORDS = ("total", "ttc", "net a payer", "net à payer", "montant")
DATE_PATTERNS = [
    re.compile(r"\b(\d{2})[/.\-](\d{2})[/.\-](\d{4})\b"),
    re.compile(r"\b(\d{2})[/.\-](\d{2})[/.\-](\d{2})\b"),
    re.compile(r"\b(\d{4})[/.\-](\d{2})[/.\-](\d{2})\b"),
]


def _ocr_image_file(image_path_or_pil):
    """Fait tourner Tesseract sur une image (chemin ou objet PIL déjà ouvert)."""
    import pytesseract
    from PIL import Image, ImageOps

    if isinstance(image_path_or_pil, str):
        img = Image.open(image_path_or_pil)
        img = ImageOps.exif_transpose(img)  # corrige l'orientation des photos de téléphone
    else:
        img = image_path_or_pil
    img = img.convert("L")  # niveaux de gris, améliore l'OCR sur tickets
    try:
        return pytesseract.image_to_string(img, lang="fra")  # pack français si installé
    except pytesseract.TesseractError:
        return pytesseract.image_to_string(img, lang="eng")  # repli si "fra" absent


def _extract_pdf(pdf_path):
    """Lit un PDF : texte natif si dispo (facture numérique), sinon OCR sur les pages rasterisées (PDF scanné)."""
    import fitz  # PyMuPDF

    text_parts = []
    doc = fitz.open(pdf_path)
    try:
        # 1) Facture numérique : le texte est déjà présent dans le PDF
        for page in doc:
            page_text = page.get_text().strip()
            if page_text:
                text_parts.append(page_text)

        native_text = "\n".join(text_parts).strip()
        if len(native_text) >= 20:
            return native_text

        # 2) PDF scanné (image sans couche texte) : on rasterise et on passe par l'OCR
        from PIL import Image

        ocr_parts = []
        for page in doc[:3]:  # 3 premières pages suffisent pour une facture/ticket
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            ocr_parts.append(_ocr_image_file(img))
        return "\n".join(ocr_parts)
    finally:
        doc.close()


def run_ocr(file_path):
    """Retourne (texte_brut, montant_detecte, date_detectee_iso). Gère images et PDF."""
    is_pdf = file_path.lower().endswith(".pdf")
    try:
        if is_pdf:
            text = _extract_pdf(file_path)
        else:
            text = _ocr_image_file(file_path)
    except ImportError:
        return "", None, None
    except Exception:
        return "", None, None

    amount = _guess_amount(text)
    date_iso = _guess_date(text)
    return text, amount, date_iso


def _guess_amount(text):
    lines = text.splitlines()
    candidates = []
    for line in lines:
        low = line.lower()
        matches = AMOUNT_RE.findall(line)
        for m in matches:
            value = float(m.replace(",", "."))
            weight = 1
            if any(k in low for k in TOTAL_KEYWORDS):
                weight = 3
            candidates.append((weight, value))
    if not candidates:
        return None
    # Priorité aux lignes contenant "total", puis au montant le plus élevé
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return round(candidates[0][1], 2)


def _guess_date(text):
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups[0]) == 4:  # yyyy-mm-dd
                y, mo, d = groups
            elif len(groups[2]) == 4:  # dd-mm-yyyy
                d, mo, y = groups
            else:  # dd-mm-yy
                d, mo, y = groups
                y = "20" + y
            date_obj = datetime(int(y), int(mo), int(d))
            return date_obj.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------------
# Routes : authentification
# ----------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not full_name or not email or len(password) < 6:
            flash("Merci de remplir tous les champs (mot de passe : 6 caractères minimum).", "danger")
            return render_template("register.html")

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("Un compte existe déjà avec cet email.", "danger")
            return render_template("register.html")

        nb_users = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        role = "treasurer" if nb_users == 0 else "member"

        db.execute(
            "INSERT INTO users (full_name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (full_name, email, generate_password_hash(password), role, datetime.utcnow().isoformat()),
        )
        db.commit()

        if role == "treasurer":
            flash("Compte créé ! En tant que premier inscrit, tu es le trésorier de l'association.", "success")
        else:
            flash("Compte créé, tu peux te connecter.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            flash(f"Bienvenue, {user['full_name']} !", "success")
            return redirect(url_for("dashboard"))
        flash("Email ou mot de passe incorrect.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Tu es déconnecté(e).", "success")
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# Routes : tableau de bord
# ----------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "treasurer":
        return redirect(url_for("treasurer_dashboard"))
    return redirect(url_for("member_dashboard"))


@app.route("/mes-notes")
@login_required
def member_dashboard():
    user = current_user()
    db = get_db()
    expenses = db.execute(
        "SELECT * FROM expenses WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()

    totals = {"pending": 0.0, "approved": 0.0, "reimbursed": 0.0}
    for e in expenses:
        if e["status"] in totals and e["amount"]:
            totals[e["status"]] += e["amount"]

    return render_template("member_dashboard.html", expenses=expenses, totals=totals)


# ----------------------------------------------------------------------------
# Routes : soumission d'une note de frais
# ----------------------------------------------------------------------------

@app.route("/nouvelle-note", methods=["GET", "POST"])
@login_required
def submit_expense():
    user = current_user()
    db = get_db()

    ocr_result = None

    if request.method == "POST":
        action = request.form.get("action")

        # Étape 1 : upload de la photo -> OCR -> pré-remplissage du formulaire
        if action == "scan":
            file = request.files.get("photo")
            if not file or file.filename == "":
                flash("Choisis une photo du justificatif.", "danger")
                return render_template("submit_expense.html", categories=CATEGORIES)
            if not allowed_file(file.filename):
                flash("Format de fichier non supporté (photo JPG/PNG/WEBP ou PDF attendu).", "danger")
                return render_template("submit_expense.html", categories=CATEGORIES)

            filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            file.save(filepath)

            raw_text, amount, date_iso = run_ocr(filepath)
            ocr_result = {
                "filename": filename,
                "amount": amount,
                "date": date_iso,
                "raw_text": raw_text,
            }
            flash(
                "Photo analysée. Vérifie le montant et la date détectés avant de valider.",
                "success",
            )
            return render_template(
                "submit_expense.html", categories=CATEGORIES, ocr=ocr_result
            )

        # Étape 2 : confirmation des champs -> enregistrement en base
        if action == "confirm":
            filename = request.form.get("photo_filename")
            category = request.form.get("category")
            description = request.form.get("description", "").strip()
            amount_raw = request.form.get("amount", "").replace(",", ".")
            expense_date = request.form.get("expense_date")
            raw_text = request.form.get("raw_text", "")

            try:
                amount = round(float(amount_raw), 2)
            except ValueError:
                amount = None

            if not category or amount is None or not expense_date:
                flash("Merci de renseigner la catégorie, le montant et la date.", "danger")
                ocr_result = {
                    "filename": filename, "amount": amount_raw,
                    "date": expense_date, "raw_text": raw_text,
                }
                return render_template("submit_expense.html", categories=CATEGORIES, ocr=ocr_result)

            db.execute(
                """INSERT INTO expenses
                   (user_id, category, description, amount, expense_date,
                    photo_filename, ocr_raw_text, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    user["id"], category, description, amount, expense_date,
                    filename, raw_text, datetime.utcnow().isoformat(),
                ),
            )
            db.commit()
            flash("Note de frais envoyée au trésorier. ✅", "success")
            return redirect(url_for("member_dashboard"))

    return render_template("submit_expense.html", categories=CATEGORIES, ocr=ocr_result)


@app.route("/note/<int:expense_id>")
@login_required
def expense_detail(expense_id):
    user = current_user()
    db = get_db()
    expense = db.execute(
        "SELECT e.*, u.full_name, u.email FROM expenses e JOIN users u ON u.id = e.user_id WHERE e.id = ?",
        (expense_id,),
    ).fetchone()

    if not expense:
        flash("Note de frais introuvable.", "danger")
        return redirect(url_for("dashboard"))

    if user["role"] != "treasurer" and expense["user_id"] != user["id"]:
        flash("Tu n'as pas accès à cette note de frais.", "danger")
        return redirect(url_for("dashboard"))

    return render_template("expense_detail.html", expense=expense)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ----------------------------------------------------------------------------
# Routes : espace trésorier
# ----------------------------------------------------------------------------

@app.route("/tresorerie")
@treasurer_required
def treasurer_dashboard():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    if status_filter == "all":
        query = """SELECT e.*, u.full_name FROM expenses e JOIN users u ON u.id = e.user_id
                    ORDER BY e.created_at DESC"""
        params = ()
    else:
        query = """SELECT e.*, u.full_name FROM expenses e JOIN users u ON u.id = e.user_id
                    WHERE e.status = ? ORDER BY e.created_at DESC"""
        params = (status_filter,)
    expenses = db.execute(query, params).fetchall()

    counts = db.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM expenses GROUP BY status"
    ).fetchall()

    return render_template(
        "treasurer_dashboard.html", expenses=expenses, counts=counts, status_filter=status_filter
    )


@app.route("/note/<int:expense_id>/statut", methods=["POST"])
@treasurer_required
def update_expense_status(expense_id):
    new_status = request.form.get("status")
    comment = request.form.get("comment", "").strip()

    if new_status not in ("approved", "rejected", "reimbursed", "pending"):
        flash("Statut invalide.", "danger")
        return redirect(url_for("treasurer_dashboard"))

    db = get_db()
    db.execute(
        "UPDATE expenses SET status = ?, reviewer_comment = ?, reviewed_at = ? WHERE id = ?",
        (new_status, comment, datetime.utcnow().isoformat(), expense_id),
    )
    db.commit()
    flash("Statut mis à jour.", "success")
    return redirect(url_for("expense_detail", expense_id=expense_id))


@app.route("/tresorerie/export.csv")
@treasurer_required
def export_csv():
    db = get_db()
    expenses = db.execute(
        """SELECT e.*, u.full_name, u.email FROM expenses e JOIN users u ON u.id = e.user_id
           ORDER BY e.expense_date ASC"""
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Date", "Membre", "Email", "Catégorie", "Description", "Montant (€)", "Statut", "Commentaire"])
    for e in expenses:
        writer.writerow([
            e["expense_date"], e["full_name"], e["email"], e["category"],
            e["description"] or "", f"{e['amount']:.2f}" if e["amount"] else "",
            e["status"], e["reviewer_comment"] or "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=notes_de_frais.csv"},
    )


# ----------------------------------------------------------------------------
# Routes : gestion des membres (trésorier uniquement)
# ----------------------------------------------------------------------------

@app.route("/membres")
@treasurer_required
def admin_users():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY full_name ASC").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/membres/<int:user_id>/role", methods=["POST"])
@treasurer_required
def toggle_role(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("Membre introuvable.", "danger")
        return redirect(url_for("admin_users"))

    if user["id"] == current_user()["id"]:
        flash("Tu ne peux pas changer ton propre rôle depuis cette page.", "warning")
        return redirect(url_for("admin_users"))

    new_role = "member" if user["role"] == "treasurer" else "treasurer"
    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    db.commit()
    flash(f"{user['full_name']} est maintenant {'trésorier' if new_role == 'treasurer' else 'membre'}.", "success")
    return redirect(url_for("admin_users"))


# ----------------------------------------------------------------------------
# Lancement
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
else:
    # Assure que la base existe aussi quand l'app est lancée via un serveur WSGI
    init_db()
