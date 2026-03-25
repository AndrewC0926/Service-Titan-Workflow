type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LOG_LEVELS: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

interface LogEntry {
  level: LogLevel;
  message: string;
  timestamp: string;
  context?: Record<string, unknown>;
}

function getConfiguredLevel(): LogLevel {
  const envLevel = process.env['LOG_LEVEL'];
  if (envLevel && envLevel in LOG_LEVELS) {
    return envLevel as LogLevel;
  }
  return 'info';
}

function shouldLog(level: LogLevel): boolean {
  return LOG_LEVELS[level] >= LOG_LEVELS[getConfiguredLevel()];
}

function formatEntry(entry: LogEntry): string {
  const base = `[${entry.timestamp}] ${entry.level.toUpperCase()} ${entry.message}`;
  if (entry.context && Object.keys(entry.context).length > 0) {
    return `${base} ${JSON.stringify(entry.context)}`;
  }
  return base;
}

function log(level: LogLevel, message: string, context?: Record<string, unknown>): void {
  if (!shouldLog(level)) return;

  const entry: LogEntry = {
    level,
    message,
    timestamp: new Date().toISOString(),
    context,
  };

  const formatted = formatEntry(entry);

  switch (level) {
    case 'error':
      process.stderr.write(formatted + '\n');
      break;
    case 'warn':
      process.stderr.write(formatted + '\n');
      break;
    default:
      process.stdout.write(formatted + '\n');
      break;
  }
}

export const logger = {
  debug: (message: string, context?: Record<string, unknown>) => log('debug', message, context),
  info: (message: string, context?: Record<string, unknown>) => log('info', message, context),
  warn: (message: string, context?: Record<string, unknown>) => log('warn', message, context),
  error: (message: string, context?: Record<string, unknown>) => log('error', message, context),
};
