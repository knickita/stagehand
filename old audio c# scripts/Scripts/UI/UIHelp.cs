using UnityEngine;

public class UIHelp : MonoBehaviour
{
    public GameObject content;
    // Start is called before the first frame update
    void Start()
    {
        content.SetActive(true);       
    }

    // Update is called once per frame
    void Update()
    {
        if (Input.GetMouseButton(0)){
            content.SetActive(false);
        }        
    }

    public void Activate(){
        content.SetActive(true);
    }
}
