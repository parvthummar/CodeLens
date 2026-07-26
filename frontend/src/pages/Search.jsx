import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import GlassCard from '../components/GlassCard';
import CodeBlock from '../components/CodeBlock';
import './Search.css';

function ResultSkeleton() {
  return (
    <div className="result-skeleton glass-card glass-card-component">
      <div className="skel-row">
        <div className="skel skel-name" />
        <div className="skel skel-badge" />
        <div className="skel skel-score" />
      </div>
      <div className="skel skel-file" />
      <div className="skel skel-line" />
      <div className="skel skel-line skel-line-short" />
    </div>
  );
}

export default function Search() {
  const { id } = useParams();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [expanded, setExpanded] = useState(new Set());
  const [projectName, setProjectName] = useState('');

  useEffect(() => {
    api.getProject(id).then((p) => setProjectName(p.name)).catch(() => {});
  }, [id]);

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setSearched(true);
    setExpanded(new Set());
    try {
      const data = await api.searchProject(id, { query, top_k: 10 });
      setResults(data.results || []);
    } catch (err) {
      console.error(err);
      setResults([]);
    } finally {
      setLoading(false);
    }
  };

  const toggleCode = (index) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const getTypeClass = (type) => {
    if (type === 'function') return 'type-function';
    if (type === 'class') return 'type-class';
    return 'type-method';
  };

  return (
    <>
      <Navbar />
      <div className="search-page">
        <Link to={`/projects/${id}`} className="search-back">← Back to Project</Link>
        <div className="search-heading">
          <h1 className="search-title">Search Code</h1>
          {projectName && <span className="search-project-name">{projectName}</span>}
        </div>

        <form className="search-bar" onSubmit={handleSearch}>
          <input
            className="input-field search-input"
            type="text"
            placeholder="e.g. 'function that handles user authentication'"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          <button className="btn-primary search-btn" type="submit" disabled={loading}>
            {loading ? 'Searching...' : 'Search'}
          </button>
        </form>

        {loading && (
          <div className="search-results">
            {[...Array(4)].map((_, i) => <ResultSkeleton key={i} />)}
          </div>
        )}

        {!loading && searched && results.length === 0 && (
          <div className="search-empty">
            <div className="search-empty-icon">🔍</div>
            <h3>No results found</h3>
            <p>Try rephrasing your query or searching for a different concept</p>
          </div>
        )}

        {!loading && results.length > 0 && (
          <>
            <p className="search-count">{results.length} result{results.length !== 1 ? 's' : ''}</p>
            <div className="search-results">
              {results.map((result, index) => (
                <GlassCard
                  key={index}
                  className="result-card"
                  style={{ animationDelay: `${index * 0.04}s` }}
                >
                  <div className="result-header">
                    <span className="result-name">{result.name}</span>
                    <span className={`result-type ${getTypeClass(result.entity_type)}`}>
                      {result.entity_type}
                    </span>
                    <div className="result-score-wrap">
                      <span className="result-score-label">{(result.score * 100).toFixed(1)}%</span>
                      <span className="score-bar">
                        <span className="score-fill" style={{ width: `${result.score * 100}%` }} />
                      </span>
                    </div>
                  </div>

                  <div className="result-file">
                    📄 {result.file_path}
                    <span className="result-lines">:{result.start_line}–{result.end_line}</span>
                  </div>

                  <div className="result-description">{result.description}</div>

                  <button
                    className="btn-ghost result-toggle"
                    onClick={() => toggleCode(index)}
                  >
                    {expanded.has(index) ? '▾ Hide Source' : '▸ View Source'}
                  </button>

                  {expanded.has(index) && (
                    <div className="result-code">
                      <CodeBlock code={result.code} language="python" />
                    </div>
                  )}
                </GlassCard>
              ))}
            </div>
          </>
        )}
      </div>
    </>
  );
}
