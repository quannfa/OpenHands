import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stopV1Conversation, updateConversationExecutionStatusInCache } from "./conversation-mutation-utils";
import { useV1ConversationStateStore } from "#/stores/v1-conversation-state-store";
import { V1ExecutionStatus } from "#/types/v1/core/base/common";

export const useV1StopConversation = () => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (variables: { conversationId: string }) =>
      stopV1Conversation(variables.conversationId),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["user", "conversations"] });
      const previousConversations = queryClient.getQueryData([
        "user",
        "conversations",
      ]);

      return { previousConversations };
    },
    onError: (_, __, context) => {
      if (context?.previousConversations) {
        queryClient.setQueryData(
          ["user", "conversations"],
          context.previousConversations,
        );
      }
    },
    onSuccess: (_, variables) => {
      useV1ConversationStateStore
        .getState()
        .setExecutionStatus(V1ExecutionStatus.PAUSED);

      updateConversationExecutionStatusInCache(
        queryClient,
        variables.conversationId,
        V1ExecutionStatus.PAUSED,
      );
    },
    onSettled: (_, __, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["user", "conversation", variables.conversationId],
      });
      queryClient.invalidateQueries({ queryKey: ["user", "conversations"] });
      queryClient.invalidateQueries({
        queryKey: ["v1-batch-get-app-conversations"],
      });
    },
  });
};
