# reports.py
# Flask Blueprint for report-related routes.
# Register in app.py with: app.register_blueprint(reports_bp)

import os
from flask import Blueprint, jsonify, send_file, abort

from config import INSTRUMENTS
from engine.monthly_reporter import generate_monthly_report, generate_monthly_equity_chart

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


@reports_bp.route("/monthly/<symbol>")
def monthly_report(symbol: str):
    """Return monthly aggregated report for a symbol as JSON."""
    valid_symbols = {i["symbol"] for i in INSTRUMENTS}
    if symbol not in valid_symbols:
        abort(400, description=f"Unknown symbol: {symbol}")

    report = generate_monthly_report(symbol)
    if not report:
        return jsonify({"error": "No data found for this month"}), 404

    return jsonify(report)


@reports_bp.route("/monthly/<symbol>/chart")
def monthly_chart(symbol: str):
    """Return monthly equity curve PNG."""
    valid_symbols = {i["symbol"] for i in INSTRUMENTS}
    if symbol not in valid_symbols:
        abort(400, description=f"Unknown symbol: {symbol}")

    img_path = generate_monthly_equity_chart(symbol)
    if not img_path or not os.path.exists(img_path):
        abort(404, description="Chart not yet generated")

    return send_file(img_path, mimetype="image/png")


@reports_bp.route("/daily/<symbol>")
def daily_list(symbol: str):
    """List all daily report files for a symbol."""
    folder = os.path.join("reports", "daily")
    if not os.path.exists(folder):
        return jsonify([])
    files = sorted([
        f for f in os.listdir(folder)
        if f.startswith(symbol) and f.endswith(".csv")
    ])
    return jsonify(files)
