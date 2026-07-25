import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import GlassCard from '../components/GlassCard';
import StatusBadge from '../components/StatusBadge';
import Modal from '../components/Modal';
import LoadingSpinner from '../components/LoadingSpinner';
import './ProjectDetail.css';

const STEPS = ['pending', 'cloning', 'indexing', 'ready'];

function getStepState(stepName, currentStatus) {
  const currentIdx = STEPS.indexOf(currentStatus);
  const stepIdx = STEPS.indexOf(stepName);

  if (currentStatus === 'failed') {
    if (stepIdx < currentIdx) return 'completed';
    if (stepIdx === currentIdx) return 'failed';
    return 'pending';
  }
  if (stepIdx < currentIdx) return 'completed';
  if (stepIdx === currentIdx) return 'active';
  return 'pending';
}

export default function ProjectDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showDelete, setShowDelete] = useState(false);

  useEffect(() => {
    api.getProject(id)
      .then(setProject)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  // Live polling for in-progress statuses
  useEffect(() => {
    if (!project) return;
    const inProgress = ['pending', 'cloning', 'indexing'];
    if (!inProgress.includes(project.status)) return;

    const interval = setInterval(async () => {
      try {
        const updated = await api.getProject(id);
        setProject(updated);
        if (!inProgress.includes(updated.status)) {
          clearInterval(interval);
        }
      } catch {
        clearInterval(interval);
      }
    }, 3000);

    return () => clearInterval(interval);
  }, [id, project?.status]);

  const handleDelete = async () => {
    await api.deleteProject(id);
    navigate('/dashboard');
  };

  if (loading) {
    return (
      <>
        <Navbar />
        <div className="project-detail">
          <LoadingSpinner message="Loading project..." />
        </div>
      </>
    );
  }

  if (!project) {
    return (
      <>
        <Navbar />
        <div className="project-detail">
          <p>Project not found.</p>
        </div>
      </>
    );
  }

  return (
    <>
      <Navbar />
      <div className="project-detail">
        <Link to="/dashboard" className="detail-back">← Back to Dashboard</Link>

        <div className="detail-header">
          <h1 className="detail-title">{project.name}</h1>
          <StatusBadge status={project.status} />
        </div>

        <a
          href={project.github_repo_url}
          target="_blank"
          rel="noopener noreferrer"
          className="detail-url"
        >
          {project.github_repo_url} ↗
        </a>

        {/* Pipeline Progress */}
        <GlassCard className="pipeline-stepper">
          {STEPS.map((step, i) => (
            <div key={step} style={{ display: 'flex', alignItems: 'center', flex: i < STEPS.length - 1 ? 1 : 'none' }}>
              <div className="pipeline-step">
                <div className={`step-circle ${getStepState(step, project.status)}`}>
                  {getStepState(step, project.status) === 'completed' ? '✓' : i + 1}
                </div>
                <span className={`step-label ${getStepState(step, project.status)}`}>
                  {step}
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <div className={`step-line ${getStepState(step, project.status) === 'completed' ? 'completed' : ''}`} />
              )}
            </div>
          ))}
        </GlassCard>

        {project.status === 'failed' && (
          <div className="detail-error">
            <div className="detail-error-title">Indexing Failed</div>
            <div>{project.error_message || 'An unknown error occurred'}</div>
          </div>
        )}

        <div className="detail-actions">
          {project.status === 'ready' && (
            <Link to={`/projects/${project.id}/search`} className="btn-primary detail-search-btn">
              Search this Project
            </Link>
          )}
          <button className="btn-ghost btn-sm btn-delete" onClick={() => setShowDelete(true)}>
            Delete Project
          </button>
        </div>

        <div className="detail-timestamps">
          <span>Created: {new Date(project.created_at).toLocaleString()}</span>
          <span>Updated: {new Date(project.updated_at).toLocaleString()}</span>
        </div>
      </div>

      <Modal
        isOpen={showDelete}
        onClose={() => setShowDelete(false)}
        onConfirm={handleDelete}
        title="Delete Project"
        message="This will permanently delete the project and all indexed data."
        confirmText="Delete"
        confirmVariant="danger"
      />
    </>
  );
}
