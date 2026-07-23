import tempfile
import asyncio
from datetime import datetime, timezone
from bson import ObjectId

from app.models.project import Project, ProjectStatus
from app.services import (
    github_service,
    parser_service,
    llm_service,
    embedding_service,
    pinecone_service
)

async def run_indexing_pipeline(project_id: str) -> None:
    dest_dir = None
    try:
        project = await Project.get(ObjectId(project_id))
        if not project:
            return
            
        project.status = ProjectStatus.CLONING
        project.updated_at = datetime.now(timezone.utc)
        await project.save()
        
        dest_dir = tempfile.mkdtemp()
        await github_service.clone_repo(project.github_repo_url, dest_dir)
        
        project.status = ProjectStatus.INDEXING
        project.updated_at = datetime.now(timezone.utc)
        await project.save()
        
        entities = parser_service.parse_codebase(dest_dir)
        if not entities:
            project.status = ProjectStatus.READY
            project.updated_at = datetime.now(timezone.utc)
            await project.save()
            return
            
        descriptions = await llm_service.generate_descriptions_batch(entities)
        embeddings = await embedding_service.embed_texts(descriptions)
        
        vectors = []
        for i, entity in enumerate(entities):
            vector_id = f"{project_id}_{i}"
            metadata = {
                "name": entity.name,
                "entity_type": entity.entity_type,
                "code": entity.source_code,
                "signature": entity.signature,
                "description": descriptions[i],
                "file_path": entity.file_path,
                "start_line": entity.start_line,
                "end_line": entity.end_line
            }
            vectors.append((vector_id, embeddings[i], metadata))
            
        await pinecone_service.upsert_vectors(project.pinecone_namespace, vectors)
        
        project.status = ProjectStatus.READY
        project.updated_at = datetime.now(timezone.utc)
        await project.save()
        
    except Exception as e:
        if 'project' in locals() and project:
            project.status = ProjectStatus.FAILED
            project.error_message = str(e)
            project.updated_at = datetime.now(timezone.utc)
            await project.save()
    finally:
        if dest_dir:
            github_service.cleanup_repo(dest_dir)
