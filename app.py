from flask import Flask, render_template, request
from loopcc import compile_source

app = Flask(__name__)


def ast_to_dict(node):
    """Convert the compiler AST into a tree that the website can display."""
    if node is None or not hasattr(node, "kind"):
        return None

    result = {
        "name": str(node.kind),
        "field": "",
        "details": [],
        "children": []
    }

    if hasattr(node, "line"):
        result["details"].append(f"Line: {node.line}")

    ignored_fields = {"kind", "line", "col", "num"}

    def add_value(value, field_name):
        if value is None:
            return

        if hasattr(value, "kind"):
            child = ast_to_dict(value)
            if child:
                child["field"] = field_name
                result["children"].append(child)

        elif isinstance(value, list):
            for index, item in enumerate(value):
                if hasattr(item, "kind"):
                    child = ast_to_dict(item)
                    if child:
                        child["field"] = f"{field_name}[{index}]"
                        result["children"].append(child)

                elif isinstance(item, tuple):
                    # Declaration entries: (name, initializer, line, column)
                    name = item[0] if len(item) > 0 else "?"
                    declaration = {
                        "name": "Declaration item",
                        "field": field_name,
                        "details": [f"Name: {name}"],
                        "children": []
                    }

                    if len(item) > 1 and item[1] is not None:
                        if hasattr(item[1], "kind"):
                            child = ast_to_dict(item[1])
                            if child:
                                child["field"] = "initializer"
                                declaration["children"].append(child)
                        else:
                            declaration["details"].append(
                                f"Initializer: {item[1]}"
                            )

                    result["children"].append(declaration)

                elif item is not None:
                    result["details"].append(f"{field_name}[{index}]: {item}")

        elif isinstance(value, (str, int, float, bool)):
            result["details"].append(f"{field_name}: {value}")

    for field_name, value in node.__dict__.items():
        if field_name in ignored_fields:
            continue
        add_value(value, field_name)

    if hasattr(node, "col"):
        result["details"].append(f"Column: {node.col}")

    return result


def classify_diagnostic(level, message):
    """
    Classify messages produced by this compiler.

    Lexer and parser errors share the same Diagnostics.error() method,
    so their category must be inferred from the message.
    """
    text = str(message).lower()
    level = str(level).lower()

    if level == "warning":
        return "Warning"

    if level == "note":
        return "Note"

    lexical_phrases = (
        "unterminated comment",
        "invalid identifier",
        "unexpected character"
    )

    if any(phrase in text for phrase in lexical_phrases):
        return "Lexical Error"

    semantic_phrases = (
        "undeclared identifier",
        "redeclaration of",
        "used outside of a loop",
        "must be a variable",
        "division by zero",
        "operands of '%'",
        "operands of '%='",
        "declaration cannot be the direct body"
    )

    if any(phrase in text for phrase in semantic_phrases):
        return "Semantic Error"

    # Parser errors generally include "expected", "found", unmatched
    # delimiters, or invalid/missing loop conditions.
    syntax_phrases = (
        "expected ",
        "found ",
        "unmatched ",
        "unclosed ",
        "empty condition",
        "expected expression"
    )

    if any(phrase in text for phrase in syntax_phrases):
        return "Syntax Error"

    if level == "error":
        return "Compiler Error"

    return "Information"


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        source="",
        result=None,
        error_message=None
    )


@app.route("/compile", methods=["POST"])
def compile_code():
    source = request.form.get("source", "")

    if not source.strip():
        return render_template(
            "index.html",
            source=source,
            result=None,
            error_message="Please enter C source code before compiling."
        )

    try:
        compilation = compile_source(source)

        # 1. Lexical analysis tokens
        tokens = []

        for token in compilation.tokens:
            if token.kind == "EOF":
                continue

            tokens.append({
                "type": token.kind,
                "value": token.value,
                "line": token.line,
                "column": token.col
            })

        # 2. Diagnostics with categories
        diagnostics = []

        for line, col, level, message in compilation.diag.items:
            diagnostics.append({
                "line": line,
                "column": col,
                "level": level,
                "category": classify_diagnostic(level, message),
                "message": str(message)
            })

        errors = sum(
            1 for item in diagnostics
            if str(item["level"]).lower() == "error"
        )

        warnings = sum(
            1 for item in diagnostics
            if str(item["level"]).lower() == "warning"
        )

        notes = sum(
            1 for item in diagnostics
            if str(item["level"]).lower() == "note"
        )

        # 3. AST
        ast_data = ast_to_dict(compilation.ast)

        # 4. Three-address code
        tac_lines = compilation.tac if compilation.tac is not None else []

        result = {
            "tokens": tokens,
            "diagnostics": diagnostics,
            "errors": errors,
            "warnings": warnings,
            "notes": notes,
            "ast": ast_data,
            "tac": tac_lines,
            "success": errors == 0 and compilation.tac is not None
        }

        return render_template(
            "index.html",
            source=source,
            result=result,
            error_message=None
        )

    except Exception:
        app.logger.exception("Unexpected compilation failure")

        return render_template(
            "index.html",
            source=source,
            result=None,
            error_message=(
                "The compiler encountered an unexpected error. "
                "Check the VS Code terminal for details."
            )
        ), 500


if __name__ == "__main__":
    app.run(debug=True)