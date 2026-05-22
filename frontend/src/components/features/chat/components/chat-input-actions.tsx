import { AgentStatus } from "#/components/features/controls/agent-status";
import { Tools } from "../../controls/tools";
import { useConversationId } from "#/hooks/use-conversation-id";
import { useV1ResumeConversation } from "#/hooks/mutation/use-v1-resume-conversation";
import { ChangeAgentButton } from "../change-agent-button";
import { SwitchProfileButton } from "../switch-profile-button";
import { useV1StopConversation } from "#/hooks/mutation/use-v1-stop-conversation";

interface ChatInputActionsProps {
  disabled: boolean;
}

export function ChatInputActions({ disabled }: ChatInputActionsProps) {
  const v1StopConversationMutation = useV1StopConversation();
  const v1ResumeConversationMutation = useV1ResumeConversation();
  const { conversationId } = useConversationId();

  const handleStopAgent = () => {
    // V1: Stop the conversation (agent execution)
    v1StopConversationMutation.mutate({ conversationId });
  };

  const handleResumeAgentClick = () => {
    // V1: Resume the conversation (agent execution)
    v1ResumeConversationMutation.mutate({ conversationId });
  };

  const isPausing = v1StopConversationMutation.isPending;

  return (
    <div className="w-full flex items-center justify-between">
      <div className="flex items-center gap-1">
        <div className="flex items-center gap-4">
          <Tools />
          <ChangeAgentButton />
          <SwitchProfileButton />
        </div>
      </div>
      <AgentStatus
        className="ml-2 md:ml-3"
        handleStop={handleStopAgent}
        handleResumeAgent={handleResumeAgentClick}
        disabled={disabled}
        isPausing={isPausing}
      />
    </div>
  );
}
