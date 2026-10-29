import boto3

client = boto3.client(
    service_name="bedrock-runtime",
    region_name="ap-southeast-2"
)

MODEL_ID = "amazon.nova-pro-v1:0"


def chat_with_assistant(
    user_message: str,
    conversation_history: list
) -> str:

    # Add user message
    conversation_history.append({
        "role": "user",
        "content": [
            {
                "text": user_message
            }
        ]
    })

    response = client.converse(
        modelId=MODEL_ID,

        system=[
            {
                "text": "You are a helpful AI assistant."
            }
        ],

        messages=conversation_history,

        inferenceConfig={
            "maxTokens": 1024,
            "temperature": 0.7
        }
    )

    assistant_message = response["output"]["message"]["content"][0]["text"]

    # Add assistant response
    conversation_history.append({
        "role": "assistant",
        "content": [
            {
                "text": assistant_message
            }
        ]
    })

    return assistant_message


def main():
    print("AI Assistant ready! Type 'quit' to exit.\n")

    history = []

    while True:
        user_input = input("You: ").strip()

        if user_input.lower() in ["quit", "exit"]:
            print("Goodbye!")
            break

        if not user_input:
            continue

        try:
            response = chat_with_assistant(
                user_input,
                history
            )

            print(f"\nAssistant: {response}\n")

        except Exception as e:
            print(f"\nError: {e}\n")


if __name__ == "__main__":
    main()