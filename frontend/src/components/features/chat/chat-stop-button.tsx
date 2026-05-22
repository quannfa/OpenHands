import PauseIcon from "#/icons/pause.svg?react";
import { cn } from "#/utils/utils";

export interface ChatStopButtonProps {
  handleStop: () => void;
  className?: string;
}

export function ChatStopButton({ handleStop, className }: ChatStopButtonProps) {
  return (
    <button
      type="button"
      onClick={handleStop}
      data-testid="stop-button"
      aria-label="Stop conversation"
      className={cn("cursor-pointer text-white", className)}
    >
      <PauseIcon className="block max-w-none w-4 h-4" />
    </button>
  );
}
