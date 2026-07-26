import React from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import './Navbar.css';

const Navbar = () => {
  const { user, logout } = useAuth();

  return (
    <nav className="navbar">
      <div className="navbar-container">
        <Link to="/dashboard" className="navbar-brand">
          <span className="navbar-brand-icon">{'</>'}</span>
          CodeSearch
        </Link>
        <div className="navbar-actions">
          {user?.email && <span className="navbar-email">{user.email}</span>}
          <button className="btn-ghost navbar-logout" onClick={logout}>Logout</button>
        </div>
      </div>
    </nav>
  );
};

export default Navbar;
