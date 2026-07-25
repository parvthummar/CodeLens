import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api/client';
import Navbar from '../components/Navbar';
import GlassCard from '../components/GlassCard';
import CodeBlock from '../components/CodeBlock';
import LoadingSpinner from '../components/LoadingSpinner';
import './Search.css';

export default function Search() {
  const { id } = useParams();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [expanded, setExpanded] = useState(new Set());

  const handleSearch = async (e) => {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      const data = await api.searchProject(id, { query, top_k: 10 });
      setResults(data.results || []);
      setExpanded(new Set());
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
        <h1 className="search-title">Search Code</h1>

        <form className="search-bar" onSubmit={handleSearch}>
          <input
            className="input-field search-input"
            type="text"
            placeholder="Search with natural language... e.g. 'function that handles authentication'"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn-primary search-btn" type="submit" disabled={loading}>
            {loading ? 'Searching...' : 'Search'}
          </button>
        </form>

        {loading && <LoadingSpinner message="Searching..." />}

        {!loading && searched && results.length === 0 && (
          <div className="search-empty">
            <h3>No results found</h3>
            <p>Try a different search query</p>
          </div>
        )}

        {!loading && results.length > 0 && (
          <div className="search-results">
            {results.map((result, index) => (
              <GlassCard
                key={index}
                className="result-card"
                style={{ animationDelay: `${index * 0.05}s` }}
              >
                <div className="result-header">
                  <span className="result-name">{result.name}</span>
                  <span className={`result-type ${getTypeClass(result.entity_type)}`}>
                    {result.entity_type}
                  </span>
                  <span className="result-score">
                    {(result.score * 100).toFixed(1)}%
                    <span className="score-bar">
                      <span className="score-fill" style={{ width: `${result.score * 100}%` }} />
                    </span>
                  </span>
                </div>

                <div className="result-file">
                  {result.file_path}:{result.start_line}-{result.end_line}
                </div>

                <div className="result-description">{result.description}</div>

                <button
                  className="btn-ghost result-toggle"
                  onClick={() => toggleCode(index)}
                >
                  {expanded.has(index) ? '▾ Hide Code' : '▸ Show Code'}
                </button>

                {expanded.has(index) && (
                  <div className="result-code">
                    <CodeBlock code={result.code} language="python" />
                  </div>
                )}
              </GlassCard>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
