import type { SVGProps } from 'react';

/**
 * Inline SVG icon set. All icons are stroke-based geometry on a 24x24 grid —
 * no emoji, no external icon font, accessible via optional <title>.
 */
export type IconName =
  | 'admin'
  | 'ai'
  | 'alertCircle'
  | 'arrowLeft'
  | 'arrowRight'
  | 'bank'
  | 'bell'
  | 'calendar'
  | 'chart'
  | 'check'
  | 'checkCircle'
  | 'chevronDown'
  | 'chevronLeft'
  | 'chevronRight'
  | 'clipboard'
  | 'clock'
  | 'close'
  | 'compare'
  | 'copy'
  | 'dashboard'
  | 'data'
  | 'database'
  | 'document'
  | 'download'
  | 'edit'
  | 'experiment'
  | 'externalLink'
  | 'eye'
  | 'filter'
  | 'flask'
  | 'gauge'
  | 'history'
  | 'inbox'
  | 'industry'
  | 'info'
  | 'jobs'
  | 'key'
  | 'layers'
  | 'link'
  | 'list'
  | 'lock'
  | 'logout'
  | 'menu'
  | 'minus'
  | 'pause'
  | 'play'
  | 'plus'
  | 'positions'
  | 'presets'
  | 'refresh'
  | 'search'
  | 'server'
  | 'settings'
  | 'shield'
  | 'sliders'
  | 'star'
  | 'starFilled'
  | 'stop'
  | 'strategies'
  | 'tag'
  | 'trading'
  | 'trash'
  | 'trendingDown'
  | 'trendingUp'
  | 'unlock'
  | 'upload'
  | 'user'
  | 'users'
  | 'wallet'
  | 'warning'
  | 'xCircle';

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'children'> {
  name: IconName;
  title?: string;
}

function IconPaths({ name }: { name: IconName }) {
  switch (name) {
    case 'dashboard':
      return (
        <>
          <rect x="3" y="3" width="8" height="10" rx="1" />
          <rect x="13" y="3" width="8" height="6" rx="1" />
          <rect x="13" y="11" width="8" height="10" rx="1" />
          <rect x="3" y="15" width="8" height="6" rx="1" />
        </>
      );
    case 'chart':
      return (
        <>
          <path d="M4 19V9m6 10V5m6 14v-7" />
          <path d="M2 19h20" />
        </>
      );
    case 'experiment':
      return (
        <>
          <path d="M9 3h6M10 3v5l-5.2 8.8A2.8 2.8 0 0 0 7.2 21h9.6a2.8 2.8 0 0 0 2.4-4.2L14 8V3" />
          <path d="M7.5 15h9" />
        </>
      );
    case 'flask':
      return (
        <>
          <path d="M10 2v6.3L4.7 19a2 2 0 0 0 1.8 3h11a2 2 0 0 0 1.8-3L14 8.3V2" />
          <path d="M8.5 2h7M7 15h10" />
        </>
      );
    case 'trading':
      return (
        <>
          <rect x="3" y="7" width="18" height="13" rx="2" />
          <path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v2h4v-2" />
        </>
      );
    case 'data':
      return (
        <>
          <ellipse cx="12" cy="5" rx="8" ry="3" />
          <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
        </>
      );
    case 'database':
      return (
        <>
          <ellipse cx="12" cy="5" rx="7" ry="2.6" />
          <path d="M5 5v14c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5" />
          <path d="M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6" />
        </>
      );
    case 'strategies':
      return (
        <>
          <path d="m12 3-9 4.5 9 4.5 9-4.5L12 3Z" />
          <path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5" />
        </>
      );
    case 'layers':
      return (
        <>
          <path d="m12 4 8 4-8 4-8-4 8-4Z" />
          <path d="m4 12 8 4 8-4M4 16l8 4 8-4" />
        </>
      );
    case 'jobs':
      return (
        <>
          <circle cx="12" cy="13" r="8" />
          <path d="M12 9v4l3 2M9 3h6" />
        </>
      );
    case 'clock':
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7v5l3.5 2" />
        </>
      );
    case 'admin':
      return (
        <>
          <circle cx="12" cy="8" r="3" />
          <path d="M5.5 20a6.5 6.5 0 0 1 13 0M19 4v4M17 6h4" />
        </>
      );
    case 'user':
      return (
        <>
          <circle cx="12" cy="8" r="3.5" />
          <path d="M5 20a7 7 0 0 1 14 0" />
        </>
      );
    case 'users':
      return (
        <>
          <circle cx="9" cy="8" r="3" />
          <path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 5.5a3 3 0 0 1 0 5.8M17.5 14.2a5.5 5.5 0 0 1 3 4.8" />
        </>
      );
    case 'clipboard':
      return (
        <>
          <rect x="5" y="4" width="14" height="17" rx="2" />
          <path d="M9 4.5V3h6v1.5M8 10h8M8 14h8M8 18h5" />
        </>
      );
    case 'document':
      return (
        <>
          <path d="M6 3h8l4 4v14H6z" />
          <path d="M14 3v4h4M9 12h6M9 16h6" />
        </>
      );
    case 'positions':
      return (
        <>
          <path d="M4 8.5 12 4l8 4.5v8L12 21l-8-4.5v-8Z" />
          <path d="m4 8.5 8 4.5 8-4.5M12 13v8" />
        </>
      );
    case 'compare':
      return (
        <>
          <path d="M7 3v15M3 7l4-4 4 4M17 21V6M13 17l4 4 4-4" />
        </>
      );
    case 'presets':
      return (
        <>
          <path d="M4 6h7M15 6h5M4 12h3M11 12h9M4 18h10M18 18h2" />
          <circle cx="13" cy="6" r="2" />
          <circle cx="9" cy="12" r="2" />
          <circle cx="16" cy="18" r="2" />
        </>
      );
    case 'sliders':
      return (
        <>
          <path d="M5 4v6M5 14v6M12 4v2M12 10v10M19 4v10M19 18v2" />
          <circle cx="5" cy="12" r="2" />
          <circle cx="12" cy="8" r="2" />
          <circle cx="19" cy="16" r="2" />
        </>
      );
    case 'inbox':
      return (
        <>
          <path d="M4 5h16v14H4z" />
          <path d="m4 13 4-4h8l4 4M4 13h5l1.5 2h3L15 13h5" />
        </>
      );
    case 'check':
      return <path d="m5 12 4 4L19 6" />;
    case 'checkCircle':
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="m8.5 12.2 2.4 2.4 4.6-5" />
        </>
      );
    case 'xCircle':
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="m9 9 6 6M15 9l-6 6" />
        </>
      );
    case 'alertCircle':
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7.5V13M12 16.2v.1" />
        </>
      );
    case 'info':
      return (
        <>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11v5.5M12 7.6v.1" />
        </>
      );
    case 'warning':
      return (
        <>
          <path d="M10.3 3.7 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z" />
          <path d="M12 9v4M12 17h.01" />
        </>
      );
    case 'lock':
      return (
        <>
          <rect x="5" y="10" width="14" height="11" rx="2" />
          <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" />
        </>
      );
    case 'unlock':
      return (
        <>
          <rect x="5" y="10" width="14" height="11" rx="2" />
          <path d="M8 10V7a4 4 0 0 1 7.8-1.2M12 14v3" />
        </>
      );
    case 'shield':
      return (
        <>
          <path d="M12 3 5 5.8v5.4c0 4.4 3 7.8 7 9.8 4-2 7-5.4 7-9.8V5.8L12 3Z" />
          <path d="m9 11.6 2.2 2.2 3.8-4" />
        </>
      );
    case 'key':
      return (
        <>
          <circle cx="8" cy="14" r="4" />
          <path d="m11 11 8-8M15 5l3 3M18 8l2-2" />
        </>
      );
    case 'bank':
      return (
        <>
          <path d="m3 9 9-6 9 6M4 9v10M20 9v10M2 19h20M8 12v4M12 12v4M16 12v4" />
        </>
      );
    case 'wallet':
      return (
        <>
          <rect x="3" y="6" width="18" height="14" rx="2" />
          <path d="M3 10h18M16 15h2" />
        </>
      );
    case 'industry':
      return (
        <>
          <path d="M3 21V8l6 4V8l6 4V5h4v16H3Z" />
          <path d="M7 17h2M12 17h2M17 17h.5" />
        </>
      );
    case 'ai':
      return (
        <>
          <rect x="6" y="6" width="12" height="12" rx="2" />
          <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
          <path d="M10 12h4" />
        </>
      );
    case 'bell':
      return (
        <>
          <path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2.5h-15L6 16Z" />
          <path d="M10 20a2.2 2.2 0 0 0 4 0" />
        </>
      );
    case 'search':
      return (
        <>
          <circle cx="11" cy="11" r="6.5" />
          <path d="m16 16 4.5 4.5" />
        </>
      );
    case 'filter':
      return <path d="M4 5h16l-6.5 7.5V19l-3 2v-8.5L4 5Z" />;
    case 'menu':
      return <path d="M4 6h16M4 12h16M4 18h16" />;
    case 'close':
      return <path d="M6 6l12 12M18 6 6 18" />;
    case 'chevronDown':
      return <path d="m6 9 6 6 6-6" />;
    case 'chevronLeft':
      return <path d="m14 6-6 6 6 6" />;
    case 'chevronRight':
      return <path d="m10 6 6 6-6 6" />;
    case 'arrowLeft':
      return <path d="M19 12H5m6-6-6 6 6 6" />;
    case 'arrowRight':
      return <path d="M5 12h14m-6-6 6 6-6 6" />;
    case 'plus':
      return <path d="M12 5v14M5 12h14" />;
    case 'minus':
      return <path d="M5 12h14" />;
    case 'edit':
      return (
        <>
          <path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17v3Z" />
          <path d="m14.5 7.5 3 3" />
        </>
      );
    case 'trash':
      return (
        <>
          <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" />
          <path d="M10 11v6M14 11v6" />
        </>
      );
    case 'copy':
      return (
        <>
          <rect x="9" y="9" width="11" height="11" rx="2" />
          <path d="M5 15V5a1 1 0 0 1 1-1h9" />
        </>
      );
    case 'download':
      return <path d="M12 3v11m0 0 4-4m-4 4-4-4M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2" />;
    case 'upload':
      return <path d="M12 14V3m0 0 4 4m-4-4L8 7M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2" />;
    case 'refresh':
      return (
        <>
          <path d="M20 12a8 8 0 1 1-2.3-5.6" />
          <path d="M20 4v4h-4" />
        </>
      );
    case 'play':
      return <path d="M7 5.5v13l11-6.5-11-6.5Z" />;
    case 'pause':
      return <path d="M8 5v14M16 5v14" />;
    case 'stop':
      return <rect x="6" y="6" width="12" height="12" rx="1.5" />;
    case 'eye':
      return (
        <>
          <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
          <circle cx="12" cy="12" r="3" />
        </>
      );
    case 'calendar':
      return (
        <>
          <rect x="4" y="5" width="16" height="16" rx="2" />
          <path d="M4 10h16M8 3v4M16 3v4" />
        </>
      );
    case 'history':
      return (
        <>
          <path d="M4 12a8 8 0 1 1 2.3 5.6" />
          <path d="M4 13v-4h4M12 8v4l3 2" />
        </>
      );
    case 'link':
      return (
        <>
          <path d="M10 14a4 4 0 0 0 6 .4l3-3a4 4 0 0 0-5.6-5.6l-1.7 1.7" />
          <path d="M14 10a4 4 0 0 0-6-.4l-3 3a4 4 0 0 0 5.6 5.6l1.7-1.7" />
        </>
      );
    case 'externalLink':
      return (
        <>
          <path d="M14 4h6v6M20 4 11 13" />
          <path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
        </>
      );
    case 'logout':
      return (
        <>
          <path d="M14 4H6v16h8M10 12h11m-3-3 3 3-3 3" />
        </>
      );
    case 'settings':
      return (
        <>
          <circle cx="12" cy="12" r="3" />
          <path d="M12 2.8 13.6 5h2.7l1.2 2.3 2.4 1.2-.3 2.6 1.6 2-1.6 2 .3 2.6-2.4 1.2-1.2 2.3h-2.7L12 21.2 10.4 19H7.7l-1.2-2.3-2.4-1.2.3-2.6-1.6-2 1.6-2L4.1 8.5l2.4-1.2L7.7 5h2.7L12 2.8Z" />
        </>
      );
    case 'server':
      return (
        <>
          <rect x="4" y="4" width="16" height="7" rx="1.5" />
          <rect x="4" y="13" width="16" height="7" rx="1.5" />
          <path d="M8 7.5h.01M8 16.5h.01" />
        </>
      );
    case 'gauge':
      return (
        <>
          <path d="M4.5 19a9 9 0 1 1 15 0" />
          <path d="M12 14l4-5" />
          <circle cx="12" cy="14" r="1.4" />
        </>
      );
    case 'tag':
      return (
        <>
          <path d="m3 12 9-9h9v9l-9 9-9-9Z" />
          <circle cx="16.5" cy="7.5" r="1.4" />
        </>
      );
    case 'star':
      return (
        <path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5Z" />
      );
    case 'starFilled':
      return (
        <path
          d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9L12 3.5Z"
          fill="currentColor"
          stroke="none"
        />
      );
    case 'trendingUp':
      return <path d="m3 17 6-6 4 4 8-8M15 7h6v6" />;
    case 'trendingDown':
      return <path d="m3 7 6 6 4-4 8 8M15 17h6v-6" />;
    case 'list':
      return <path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01" />;
    default:
      return null;
  }
}

export default function Icon({
  name,
  title,
  className = 'h-5 w-5',
  ...props
}: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
      {...props}
    >
      {title && <title>{title}</title>}
      <IconPaths name={name} />
    </svg>
  );
}
