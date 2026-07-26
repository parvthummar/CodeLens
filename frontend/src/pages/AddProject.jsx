import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import './AddProject.css';

const GITHUB_RE = /^https?:\/\/(www\.)?github\.com\/[\w.-]+\/[\w.-]+(\.git)?\/?$/i;

export default function AddProject() {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (!GITHUB_RE.test(url.trim())) {
      setError('Please enter a valid GitHub repository URL (e.g. https://github.com/owner/repo)');
      return;
    }

    setLoading(true);
    try {
      const project = await api.createProject({
        github_repo_url: url.trim(),
        name: name.trim() || undefined,
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
          <div className="add-project-icon">{'</>'}</div>
          <h1 className="add-project-title">Add Project</h1>
          <p className="add-project-subtitle">
            Connect a public GitHub repository to index its Python code for natural language search
          </p>

          {error && <div className="login-error add-project-error">{error}</div>}

          <form className="add-project-form" onSubmit={handleSubmit}>
            <div className="field-group">
              <label className="field-label">GitHub Repository URL *</label>
              <input
                className="input-field"
                type="text"
                placeholder="https://github.com/owner/repo"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required
              />
            </div>
            <div className="field-group">
              <label className="field-label">Project Name <span className="field-optional">(optional)</span></label>
              <input
                className="input-field"
                type="text"
                placeholder="My Awesome Project"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <button className="btn-primary login-submit" type="submit" disabled={loading}>
              {loading ? 'Creating...' : 'Add Project'}
            </button>
          </form>

          <Link to="/dashboard" className="add-project-back">← Back to Dashboard</Link>
        </div>
      </div>
    </>
  );
}
