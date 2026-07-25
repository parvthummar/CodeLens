import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import GlassCard from '../components/GlassCard';
import StatusBadge from '../components/StatusBadge';
import Modal from '../components/Modal';
import LoadingSpinner from '../components/LoadingSpinner';
import './Dashboard.css';

export default function Dashboard() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [deleteId, setDeleteId] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    api.getProjects()
      .then(setProjects)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      await api.deleteProject(deleteId);
      setProjects((prev) => prev.filter((p) => p.id !== deleteId));
    } catch (err) {
      console.error(err);
    }
    setDeleteId(null);
  };

  if (loading) {
    return (
      <>
        <Navbar />
        <div className="dashboard">
          <LoadingSpinner message="Loading projects..." />
        </div>
      </>
    );
  }

  return (
    <>
      <Navbar />
      <div className="dashboard">
        <div className="dashboard-header">
          <h1 className="dashboard-title">Your Projects</h1>
          <Link to="/projects/new" className="btn-primary btn-sm" style={{ textDecoration: 'none' }}>
            <span className="add-btn-icon">+</span> Add Project
          </Link>
        </div>

        {projects.length === 0 ? (
          <div className="empty-state">
            <h2>No projects yet</h2>
            <p>Connect a GitHub repository to start searching code with AI</p>
            <Link to="/projects/new" className="btn-primary" style={{ textDecoration: 'none' }}>
              Add Your First Project
            </Link>
          </div>
        ) : (
          <div className="dashboard-grid">
            {projects.map((project) => (
              <GlassCard key={project.id} className="project-card">
                <div className="project-name">{project.name}</div>
                <div className="project-url">{project.github_repo_url}</div>
                <div className="project-meta">
                  <StatusBadge status={project.status} />
                  <span className="project-date">
                    {new Date(project.created_at).toLocaleDateString()}
                  </span>
                </div>
                <div className="project-actions">
                  {project.status === 'ready' && (
                    <Link
                      to={`/projects/${project.id}/search`}
                      className="btn-primary btn-sm"
                      style={{ textDecoration: 'none' }}
                    >
                      Search
                    </Link>
                  )}
                  <Link
                    to={`/projects/${project.id}`}
                    className="btn-ghost btn-sm"
                    style={{ textDecoration: 'none' }}
                  >
                    Details
                  </Link>
                  <button
                    className="btn-ghost btn-sm btn-delete"
                    onClick={() => setDeleteId(project.id)}
                  >
                    Delete
                  </button>
                </div>
              </GlassCard>
            ))}
          </div>
        )}
      </div>

      <Modal
        isOpen={!!deleteId}
        onClose={() => setDeleteId(null)}
        onConfirm={handleDelete}
        title="Delete Project"
        message="This will permanently delete the project and all its indexed data. This action cannot be undone."
        confirmText="Delete"
        confirmVariant="danger"
      />
    </>
  );
}
