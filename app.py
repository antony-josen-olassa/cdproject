
from flask import Flask, render_template, request, jsonify
from loopcc import compile_source

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/compile", methods=["POST"])
def compile_code():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")

    try:
        result = compile_source(code)
        diagnostics = [
            {
                "line": line,
                "column": col,
                "level": level,
                "message": message
            }
            for line, col, level, message in result.diag.items
        ]

        return jsonify({
            "errors": result.diag.errors,
            "warnings": result.diag.warnings,
            "diagnostics": diagnostics,
            "tac": result.tac or []
        })
    except Exception:
        app.logger.exception("Compilation failed")
        return jsonify({"error": "Compilation failed"}), 500

if __name__ == "__main__":
    app.run(debug=True)
