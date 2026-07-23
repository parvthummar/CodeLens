import ast
import os
import pathlib
from dataclasses import dataclass

@dataclass
class CodeEntity:
    name: str
    entity_type: str
    source_code: str
    signature: str
    file_path: str
    start_line: int
    end_line: int

def _build_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    for arg in node.args.posonlyargs:
        args.append(arg.arg)
    for arg in node.args.args:
        args.append(arg.arg)
    args_str = ", ".join(args)
    returns = ""
    if hasattr(node, 'returns') and node.returns:
        returns = " -> ..."
    return f"def {node.name}({args_str}){returns}:"

def _build_class_signature(node: ast.ClassDef) -> str:
    bases = [getattr(b, 'id', '...') for b in node.bases if isinstance(b, ast.Name)]
    bases_str = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{bases_str}:"

def parse_codebase(repo_dir: str) -> list[CodeEntity]:
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv", "env", ".tox", ".eggs"}
    entities = []
    
    for py_file in pathlib.Path(repo_dir).rglob("*.py"):
        parts = py_file.parts
        if any(skip_dir in parts for skip_dir in skip_dirs) or any(part.endswith('.egg-info') for part in parts):
            continue
            
        try:
            content = py_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
            
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
            
        lines = content.splitlines()
        file_path_rel = os.path.relpath(py_file, repo_dir)
        
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entity_type = "function"
                name = node.name
                start_line = node.lineno
                end_line = getattr(node, 'end_lineno', start_line)
                source_code = ast.get_source_segment(content, node) or "\n".join(lines[start_line-1:end_line])
                signature = _build_func_signature(node)
                entities.append(CodeEntity(name, entity_type, source_code, signature, file_path_rel, start_line, end_line))
            elif isinstance(node, ast.ClassDef):
                entity_type = "class"
                class_name = node.name
                start_line = node.lineno
                end_line = getattr(node, 'end_lineno', start_line)
                source_code = ast.get_source_segment(content, node) or "\n".join(lines[start_line-1:end_line])
                signature = _build_class_signature(node)
                entities.append(CodeEntity(class_name, entity_type, source_code, signature, file_path_rel, start_line, end_line))
                
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_name = f"{class_name}.{child.name}"
                        m_start = child.lineno
                        m_end = getattr(child, 'end_lineno', m_start)
                        m_source = ast.get_source_segment(content, child) or "\n".join(lines[m_start-1:m_end])
                        m_sig = _build_func_signature(child)
                        entities.append(CodeEntity(m_name, "method", m_source, m_sig, file_path_rel, m_start, m_end))

    return entities
