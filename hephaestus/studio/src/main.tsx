import { createRoot } from 'react-dom/client';
import App from './App';

const host = document.getElementById('root');
if (!host) throw new Error('#root missing');
createRoot(host).render(<App />);
