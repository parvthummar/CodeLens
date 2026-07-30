import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import GlassCard from '../components/GlassCard';
import StatusBadge from '../components/StatusBadge';
import Modal from '../components/Modal';
import LoadingSpinner from '../components/LoadingSpinner';
import './ProjectDetail.css';

const STEPS = ['queued', 'cloning', 'indexing', 'ready'];
const STEP_LABELS = { queued: 'Queued', cloning: 'Cloning', indexing: 'Indexing', ready: 'Ready' };

// Projects created before the worker existed still carry 'pending', which meant
// the same thing: waiting to be picked up.
const normalize = (status) => (status === 'pending' ? 'queued' : status);

const IN_PROGRESS = ['queued', 'cloning', 'indexing'];

function getStepState(stepName, status) {
  const currentStatus = normalize(status);
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
  const [reindexing, setReindexing] = useState(false);
  const [reindexError, setReindexError] = useState(null);

  useEffect(() => {
    api.getProject(id)
      .then(setProject)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (!project) return;
    if (!IN_PROGRESS.includes(normalize(project.status))) return;

    const interval = setInterval(async () => {
      try {
        const updated = await api.getProject(id);
        setProject(updated);
        if (!IN_PROGRESS.includes(normalize(updated.status))) clearInterval(interval);
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

  const handleReindex = async () => {
    setReindexing(true);
    setReindexError(null);
    try {
      // The response already carries the queued status, so the stepper resets
      // immediately and the polling effect above restarts on its own.
      setProject(await api.reindexProject(id));
    } catch (e) {
      setReindexError(e.message);
    } finally {
      setReindexing(false);
    }
  };

  if (loading) {
    return (
      <>
        <Navbar />
        <div className="project-detail project-detail-loading">
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
          <Link to="/dashboard" className="detail-back">← Back to Dashboard</Link>
          <p className="detail-not-found">Project not found.</p>
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
          <div>
            <h1 className="detail-title">{project.name}</h1>
            <a
              href={project.github_repo_url}
              target="_blank"
              rel="noopener noreferrer"
              className="detail-url"
            >
              {project.github_repo_url} ↗
            </a>
          </div>
          <StatusBadge status={project.status} />
        </div>

        {/* Pipeline Progress */}
        <GlassCard className="pipeline-stepper">
          {STEPS.map((step, i) => (
            <div key={step} className="pipeline-item">
              <div className="pipeline-step">
                <div className={`step-circle step-${getStepState(step, project.status)}`}>
                  {getStepState(step, project.status) === 'completed' ? '✓' : i + 1}
                </div>
                <span className={`step-label step-label-${getStepState(step, project.status)}`}>
                  {STEP_LABELS[step]}
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <div className={`step-line ${getStepState(step, project.status) === 'completed' ? 'step-line-done' : ''}`} />
              )}
            </div>
          ))}
        </GlassCard>

        {project.status === 'failed' && (
          <div className="detail-error">
            <div className="detail-error-title">⚠ Indexing Failed</div>
            <div className="detail-error-msg">{project.error_message || 'An unknown error occurred'}</div>
          </div>
        )}

        {reindexError && (
          <div className="detail-error">
            <div className="detail-error-msg">{reindexError}</div>
          </div>
        )}

        {IN_PROGRESS.includes(normalize(project.status)) && (
          <div className="detail-progress-note">
            {normalize(project.status) === 'queued'
              ? 'Waiting for a worker — this page auto-refreshes every 3 seconds'
              : 'Indexing in progress — this page auto-refreshes every 3 seconds'}
          </div>
        )}

        <div className="detail-actions">
          {project.status === 'ready' && (
            <Link to={`/projects/${project.id}/search`} className="btn-primary detail-search-btn" style={{ textDecoration: 'none' }}>
              Search this Project
            </Link>
          )}
          {!IN_PROGRESS.includes(normalize(project.status)) && (
            <button className="btn-ghost btn-sm" onClick={handleReindex} disabled={reindexing}>
              {reindexing ? 'Queueing…' : 'Re-index'}
            </button>
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
        message="This will permanently delete the project and all indexed data. This action cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
      />
    </>
  );
}
