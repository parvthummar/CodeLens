import React from 'react';
import './GlassCard.css';

const GlassCard = ({ children, className = '', onClick, style }) => {
  return (
    <div
      className={`glass-card-component glass-card ${className}`}
      onClick={onClick}
      style={style}
    >
      {children}
    </div>
  );
};

export default GlassCard;
