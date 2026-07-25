import React, { useEffect, useState } from 'react';
import Prism from 'prismjs';
import 'prismjs/themes/prism-tomorrow.css';
import 'prismjs/components/prism-python';
import './CodeBlock.css';

const CodeBlock = ({ code, language = 'python' }) => {
  const [highlightedCode, setHighlightedCode] = useState('');

  useEffect(() => {
    try {
      const html = Prism.highlight(code, Prism.languages[language] || Prism.languages.python, language);
      setHighlightedCode(html);
    } catch (e) {
      setHighlightedCode(code);
    }
  }, [code, language]);

  return (
    <div className="code-block-container">
      <pre>
        <code 
          className={`language-${language}`}
          dangerouslySetInnerHTML={{ __html: highlightedCode || code }}
        />
      </pre>
    </div>
  );
};

export default CodeBlock;
