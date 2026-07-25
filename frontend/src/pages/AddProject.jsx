import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import './AddProject.css';

export default function AddProject() {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const project = await api.createProject({
        github_repo_url: url,
        name: name || undefined,
      });
      navigate(`/projects/${project.id}`);
    } catch (err) {
      setError(err.message || 'Failed to create project');
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <Navbar />
      <div className="add-project-page">
        <div className="glass-card add-project-card">
          <h1 className="add-project-title">Add Project</h1>
          <p className="add-project-subtitle">Connect a GitHub repository</p>

          {error && <div className="login-error">{error}</div>}

          <form className="add-project-form" onSubmit={handleSubmit}>
            <input
              className="input-field"
              type="url"
              placeholder="https://github.com/owner/repo"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
            />
            <input
              className="input-field"
              type="text"
              placeholder="Optional custom name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <button className="btn-primary login-submit" type="submit" disabled={loading}>
              {loading ? 'Creating...' : 'Add Project'}
            </button>
          </form>

          <Link to="/dashboard" className="add-project-back">
            ← Back to Dashboard
          </Link>
        </div>
      </div>
    </>
  );
}
